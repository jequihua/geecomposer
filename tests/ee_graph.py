"""Resolve serialized EE references and inspect actual dependency paths."""

import json


class Graph:
    def __init__(self, image):
        graph = json.loads(image.serialize())
        self.values = graph["values"]
        self.root = self.values[graph["result"]]

    def resolve(self, node):
        while "valueReference" in node:
            node = self.values[node["valueReference"]]
        return node

    def calls(self, name, node=None):
        """Yield matching ancestors only, never unrelated serialized values."""
        pending = [self.root if node is None else node]
        seen = set()
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                item = self.resolve(item)
                if id(item) in seen:
                    continue
                seen.add(id(item))
                call = item.get("functionInvocationValue")
                if call and call["functionName"] == name:
                    yield call["arguments"]
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)

    def constant(self, node):
        node = self.resolve(node)
        if "arrayValue" in node:
            return [self.constant(x) for x in node["arrayValue"]["values"]]
        return node["constantValue"]
