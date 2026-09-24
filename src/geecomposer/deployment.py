"""Small operator tool. Default plan is offline; mutations require explicit modes.

This is not the hosted runner and never grants cloud authority or logs in.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from .recipe import (
    RunnerError,
    allowed,
    canonical,
    digest,
    read_json,
    require,
    validate_policy,
    validate_recipe,
)
from .runner import run_id


def hosted_policy(value):
    policy = validate_policy(value)
    require(policy["mode"] == "hosted", "administrator hosted policy required")
    # Exact shipped example values, not substrings of institutional names.
    examples = {
        "geecomposer-example",
        "worker@geecomposer-example.iam.gserviceaccount.com",
        "geecomposer-example-input",
        "geecomposer-example-output",
        "geecomposer-example-control",
        "replace-with-reviewed-image-digest",
    }
    values = [policy[k] for k in ("project", "service_account", "image_version")]
    for resource in [*policy["inputs"], *policy["outputs"], policy["control"]]:
        values.extend((resource["bucket"], resource["prefix"]))
    require(
        all(
            value not in examples
            and not re.search(r"\$\{[^{}]+\}|<[^<>]+>|\{\{[^{}]+\}\}|__[A-Z][A-Z0-9_]*__", value)
            for value in values
        ),
        "unresolved illustrative policy values",
    )
    return policy


def identifier(value, pattern, message):
    require(type(value) is str and re.fullmatch(pattern, value), message)
    return value


def input_uri(uri, policy):
    parsed = urlsplit(uri)
    name = parsed.path.removeprefix("/")
    require(
        parsed.scheme == "gs"
        and not parsed.query
        and not parsed.fragment
        and len(name) <= 512
        and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", name)
        and all(p not in (".", "..") for p in name.split("/")),
        "invalid immutable input URI",
    )
    require(
        allowed({"bucket": parsed.netloc, "prefix": name}, policy["inputs"]), "input denied", "auth"
    )
    return parsed.netloc, name


def job_spec(template, policy, image, region, job, project_number):
    identifier(project_number, r"[1-9][0-9]{5,19}", "explicit project number required")
    identifier(
        image,
        r"[a-z][a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9_-]+/[a-z0-9_/-]+@sha256:[0-9a-f]{64}",
        "immutable Artifact Registry image digest required",
    )
    identifier(region, r"[a-z]+-[a-z]+[0-9]+", "invalid region")
    identifier(job, r"[a-z][a-z0-9-]{0,61}[a-z0-9]", "invalid job")
    values = {
        "PROJECT_NUMBER": project_number,
        "REGION": region,
        "JOB": job,
        "IMAGE": image,
        "RUNTIME_ACCOUNT": policy["service_account"],
        "POLICY_SHA256": digest(policy),
    }

    def replace(node):
        if isinstance(node, dict):
            return {k: replace(v) for k, v in node.items()}
        if isinstance(node, list):
            return [replace(v) for v in node]
        if isinstance(node, str) and node.startswith("${"):
            require(node[2:-1] in values and node.endswith("}"), "unknown job parameter")
            return values[node[2:-1]]
        return node

    spec = replace(template)
    # Reject unsafe edits to the supplied deployment template before producing commands.
    require(
        spec["metadata"]["name"] == job
        and spec["metadata"]["namespace"] == project_number
        and spec["metadata"]["labels"]["cloud.googleapis.com/location"] == region
        and spec["metadata"]["annotations"]["geecomposer-policy-sha256"] == digest(policy),
        "job identity/policy mismatch",
    )
    task = spec["spec"]["template"]["spec"]["template"]["spec"]
    (container,) = task["containers"]
    require(
        spec["apiVersion"] == "run.googleapis.com/v1"
        and spec["kind"] == "Job"
        and spec["spec"]["template"]["spec"]["taskCount"] == 1
        and spec["spec"]["template"]["spec"]["parallelism"] == 1
        and task["maxRetries"] == 0
        and task["timeoutSeconds"] == "2100"
        and task["serviceAccountName"] == policy["service_account"]
        and container
        == {
            "image": image,
            "args": ["--help"],
            "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
        },
        "job template violates reviewed runtime envelope",
    )
    return spec


def plan(
    policy,
    template,
    *,
    image,
    project_number,
    region,
    job,
    action,
    identifier_value,
    recipe=None,
    uri=None,
    receipt=None,
):
    identifier_value = run_id(identifier_value)
    spec = job_spec(template, policy, image, region, job, project_number)
    require(action in ("run", "status", "cancel", "resume"), "invalid runner action")
    args = [action, "--run-id", identifier_value]
    body = None
    if action == "run":
        recipe = validate_recipe(recipe, policy)
        body = canonical(recipe)
        input_uri(uri, policy)
        if receipt is not None:
            require(
                set(receipt) == {"uri", "generation", "recipe_sha256", "policy_sha256"}
                and receipt["uri"] == uri
                and receipt["recipe_sha256"] == digest(recipe)
                and receipt["policy_sha256"] == digest(policy)
                and type(receipt["generation"]) is int
                and receipt["generation"] > 0,
                "receipt does not bind this immutable recipe and policy",
            )
            args += ["--recipe", uri, "--recipe-generation", str(receipt["generation"])]
        else:
            args += ["--recipe", uri, "--recipe-generation", "GENERATION_AFTER_UPLOAD"]
    return {
        "mode": "plan",
        "project": policy["project"],
        "job": spec,
        "runner_args": args,
        "policy_sha256": digest(policy),
        "recipe_sha256": digest(recipe) if body else None,
        "ready_to_execute": action != "run" or receipt is not None,
    }


def upload(policy, recipe, uri, *, client_factory=None):
    recipe = validate_recipe(recipe, policy)
    bucket, name = input_uri(uri, policy)
    # Operator credentials are separate from attached runtime credentials. No login.
    if client_factory is None:
        from google.cloud.storage import Client

        client_factory = Client
    try:
        blob = client_factory(project=policy["project"]).bucket(bucket).blob(name)
        blob.upload_from_string(
            canonical(recipe),
            content_type="application/json",
            if_generation_match=0,
            retry=None,
            timeout=10,
        )
        require(blob.generation is not None, "upload generation unavailable", "uncertain")
        return {
            "uri": uri,
            "generation": int(blob.generation),
            "recipe_sha256": digest(recipe),
            "policy_sha256": digest(policy),
        }
    except Exception:
        raise RunnerError(
            "uncertain", "upload not confirmed; inspect original object, do not blindly repeat"
        ) from None


def execute(prepared, *, command=subprocess.run):
    require(prepared["ready_to_execute"], "generation-pinned upload receipt required")
    spec = prepared["job"]
    # Flags JSON avoids shell/list delimiter quoting; caller strings never become code.
    flags = {
        "--project": prepared["project"],
        "--region": spec["metadata"]["labels"]["cloud.googleapis.com/location"],
        "--args": prepared["runner_args"],
        "--format": "json",
        "--quiet": None,
    }
    job = spec["metadata"]["name"]
    with tempfile.TemporaryDirectory(prefix="geecomposer-submit-") as scratch:
        Path(scratch, "flags.json").write_bytes(canonical(flags))
        try:
            describe_flags = {k: v for k, v in flags.items() if k != "--args"}
            Path(scratch, "describe.json").write_bytes(canonical(describe_flags))
            described = command(
                ["gcloud", "run", "jobs", "describe", job, "--flags-file=describe.json"],
                cwd=scratch,
                shell=False,
                timeout=30,
                capture_output=True,
                text=True,
            )
            require(
                described.returncode == 0 and len(described.stdout) <= 1_048_576,
                "deployed job could not be verified",
                "uncertain",
            )
            live = json.loads(described.stdout)
            desired = spec["spec"]["template"]["spec"]
            actual = live["spec"]["template"]["spec"]
            task = actual["template"]["spec"]
            (container,) = task["containers"]
            require(
                live["metadata"]["namespace"] == spec["metadata"]["namespace"]
                and live["metadata"]["annotations"].get("geecomposer-policy-sha256")
                == prepared["policy_sha256"]
                and actual.get("taskCount", 1) == desired["taskCount"]
                and actual.get("parallelism", 1) == desired["parallelism"]
                and task.get("maxRetries") == 0
                and str(task.get("timeoutSeconds")) == "2100"
                and task.get("serviceAccountName")
                == desired["template"]["spec"]["serviceAccountName"]
                and container["image"] == desired["template"]["spec"]["containers"][0]["image"]
                and container["resources"]["limits"].get("cpu") in ("1", "1000m")
                and container["resources"]["limits"].get("memory") == "1Gi"
                and not any(container.get(k) for k in ("env", "command", "volumeMounts", "ports"))
                and not task.get("volumes"),
                "deployed job differs from reviewed policy/image/envelope",
            )
            result = command(
                ["gcloud", "run", "jobs", "execute", job, "--flags-file=flags.json"],
                cwd=scratch,
                shell=False,
                timeout=60,
                capture_output=True,
                text=True,
            )
            require(result.returncode == 0, "job submission not confirmed", "uncertain")
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            raise RunnerError(
                "uncertain", "job submission not confirmed; inspect executions before retry"
            ) from None
    # Do not log raw cloud diagnostics or pretend asynchronous execution completed.
    return {"status": "submitted", "run_id": prepared["runner_args"][2], "job": job}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("plan", "render", "policy", "upload", "execute"), default="plan"
    )
    parser.add_argument("--policy", required=True)
    parser.add_argument("--output")
    parser.add_argument("--job-template")
    parser.add_argument("--image")
    parser.add_argument("--region")
    parser.add_argument("--project-number")
    parser.add_argument("--job")
    parser.add_argument("--action", choices=("run", "status", "cancel", "resume"), default="run")
    parser.add_argument("--run-id")
    parser.add_argument("--recipe")
    parser.add_argument("--uri")
    parser.add_argument("--receipt")
    args = parser.parse_args(argv)
    try:
        require(not args.output or not Path(args.output).exists(), "output already exists")
        policy = hosted_policy(read_json(args.policy))
        if args.mode == "policy":
            require(args.output is not None, "explicit private policy output required")
            result = policy
        else:
            recipe = read_json(args.recipe) if args.recipe else None
            prepared = plan(
                policy,
                read_json(args.job_template),
                image=args.image,
                region=args.region,
                project_number=args.project_number,
                job=args.job,
                action=args.action,
                identifier_value=args.run_id,
                recipe=recipe,
                uri=args.uri,
                receipt=read_json(args.receipt) if args.receipt else None,
            )
            if args.mode == "render":
                result = prepared["job"]
            elif args.mode == "upload":
                require(args.action == "run", "upload applies only to run")
                result = upload(policy, recipe, args.uri)
            elif args.mode == "execute":
                result = execute(prepared)
            else:
                result = prepared
        if args.output:
            # Never overwrite policy, recipe, prior evidence or an existing receipt.
            with Path(args.output).open("xb") as stream:
                stream.write(canonical(result) + b"\n")
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    except (RunnerError, OSError, TypeError, KeyError, ValueError):
        print(
            json.dumps(
                {
                    "status": "unconfirmed",
                    "message": "preparation or explicit action failed; inspect private inputs and original remote state before retry",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
