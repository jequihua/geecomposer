"""Installed CLI. Parsing and offline validation never discover credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal

from .recipe import (
    EXIT_CODES,
    RunnerError,
    estimate,
    read_json,
    require,
    validate_policy,
    validate_recipe,
)

# S03 must bundle this file into a read-only image layer, outside caller mounts.
# Deliberately absent from this distribution: there is no permissive policy.
BUNDLED_POLICY = Path(__file__).with_name("runtime_policy.json")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise repeats caller strings, potentially including secrets.
        raise RunnerError("validation", "invalid command arguments; see --help")


def parser():
    result = Parser(
        prog="geecomposer",
        description="Validate and execute bounded Earth Engine recipes",
    )
    result.add_argument(
        "--policy",
        help="trusted local-operator policy file; forbidden in bundled hosted mode",
    )
    result.add_argument(
        "--hosted",
        action="store_true",
        help="require a bundled, read-only hosted policy",
    )
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    validate = commands.add_parser(
        "validate", help="offline strict JSON/resource validation"
    )
    validate.add_argument("recipe")
    run = commands.add_parser(
        "run", help="execute a new reserved run, or estimate it offline"
    )
    run.add_argument("--recipe", required=True)
    run.add_argument("--recipe-generation", type=int)
    run.add_argument("--run-id")
    run.add_argument("--dry-run", action="store_true")
    for command in ("status", "cancel", "resume"):
        sub = commands.add_parser(command)
        sub.add_argument("--run-id", required=True)
    return result


def main(argv=None, *, connector=None, budget_factory=None):
    try:
        args = parser().parse_args(argv)
        bundled = BUNDLED_POLICY.is_file()
        hosted = bundled or args.hosted
        require(
            not (hosted and args.policy), "hosted callers cannot supply policy paths"
        )
        require(not hosted or bundled, "hosted policy is not bundled")
        policy = (
            validate_policy(read_json(BUNDLED_POLICY if bundled else args.policy))
            if bundled or args.policy
            else None
        )
        if policy:
            require(
                policy["mode"] == ("hosted" if hosted else "local"),
                "policy execution mode mismatch",
            )
        offline = args.command == "validate" or (args.command == "run" and args.dry_run)
        if offline:
            require(
                not hosted,
                "offline validation requires a local copy outside hosted execution",
            )
            require(
                not args.recipe.startswith(("gs:", "http:", "https:")),
                "offline commands require a local JSON file",
            )
            require(
                not getattr(args, "recipe_generation", None),
                "generation applies only to hosted input",
            )
            recipe = validate_recipe(
                read_json(
                    args.recipe,
                    policy["limits"]["recipe_bytes"] if policy else 1_048_576,
                ),
                policy,
            )
            result = estimate(recipe)
            if args.command == "validate":
                result["status"] = "validated"
        else:
            require(
                policy is not None, "online execution requires administrator policy"
            )
            require(
                getattr(args, "run_id", None),
                "online commands require an explicit run ID",
            )
            from .runner import Runner, run_id
            from .runner_cloud import Budget, connect

            run_id(args.run_id)
            # Validate local content before identity discovery or remote reads.
            recipe, source = None, None
            if args.command == "run":
                if hosted:
                    require(
                        args.recipe.startswith("gs://")
                        and args.recipe_generation is not None,
                        "hosted recipes require gs URI and object generation",
                    )
                else:
                    require(
                        not args.recipe.startswith(("gs:", "http:", "https:"))
                        and args.recipe_generation is None,
                        "local recipes require a bounded JSON file",
                    )
                    recipe = validate_recipe(
                        read_json(args.recipe, policy["limits"]["recipe_bytes"]), policy
                    )
            budget = (budget_factory or Budget)(policy["limits"]["deadline_seconds"])
            store, engine, credentials = (connector or connect)(policy, budget)
            runner = Runner(policy, store, engine, credentials, budget)
            if args.command == "run" and hosted:
                value, source = store.read_recipe(args.recipe, args.recipe_generation)
                recipe = validate_recipe(value, policy)
            handlers = {}
            try:
                # Signal handlers only set a flag. Normal fenced control flow
                # performs cancellation; no network or JSON writes in a handler.
                def cancelled(signum, frame):
                    runner.cancel_requested = True

                for sig in (signal.SIGINT, signal.SIGTERM):
                    handlers[sig] = signal.signal(sig, cancelled)
                if args.command == "run":
                    result = runner.run(recipe, args.run_id, source)
                else:
                    result = getattr(runner, args.command)(args.run_id)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return EXIT_CODES[result["status"]]
    except RunnerError as exc:
        print(json.dumps({"status": exc.status, "message": str(exc)}, sort_keys=True))
        return exc.code
    except (Exception, KeyboardInterrupt):
        print(
            json.dumps(
                {
                    "status": "uncertain",
                    "message": "operation interrupted or unavailable; inspect run status",
                }
            )
        )
        return EXIT_CODES["uncertain"]


if __name__ == "__main__":
    raise SystemExit(main())
