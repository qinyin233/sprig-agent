"""Keep the best literal name/path matches within a serialized output budget."""

from dataclasses import dataclass
import heapq
import json


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def match_score(value, query, separator="."):
    folded = value.casefold()
    leaf = folded.rsplit(separator, 1)[-1]
    if folded == query or leaf == query:
        return 3
    if folded.startswith(query) or leaf.startswith(query):
        return 2
    return int(query in folded)


@dataclass
class Candidate:
    score: int
    record: dict
    matches: int
    size: int

    def __lt__(self, other):
        # The heap root is the worst candidate; ties prefer path then line.
        if self.score != other.score:
            return self.score < other.score
        if self.record["path"] != other.record["path"]:
            return self.record["path"] > other.record["path"]
        return (self.line, self.column) > (other.line, other.column)

    @property
    def line(self):
        symbols = self.record.get("symbols", [])
        return symbols[0]["line"] if symbols else 0

    @property
    def column(self):
        symbols = self.record.get("symbols", [])
        return symbols[0].get("column", 0) if symbols else 0


class MapSelection:
    def __init__(self, max_output, measure=encoded_size, *, include_calls=False):
        self.heap = []
        self.size = 0
        self.measure = measure
        self.include_calls = include_calls
        self.extra = {"calls_are_syntactic": True} if include_calls else {}
        # At most 5,000 files can be visited. Reserve the complete envelope.
        self.budget = max_output - measure({"files": [], "truncated": False, "matched_files": 5000, **self.extra})
        self.truncated = False
        self.matched_files = 0

    def add(self, record, query, check_cancelled):
        path_score = match_score(record["path"], query, "/")
        if "symbols" in record:
            matches = []
            for symbol in record["symbols"]:
                check_cancelled()
                name = symbol["name"].replace("::", ".").replace("r#", "") if self.include_calls else symbol["name"]
                score = max(path_score, match_score(name, query))
                if score:
                    if self.include_calls:
                        score = score * 2 + (symbol.get("kind") != "call")
                    matches.append((score, symbol))
            if matches:
                self.matched_files += 1
                for score, symbol in matches:
                    check_cancelled()
                    self._keep(score, {"path": record["path"], "symbols": [symbol], "symbols_filtered": True}, len(matches))
            elif path_score:
                self.matched_files += 1
                self._keep(path_score, {"path": record["path"], "symbols": [], "symbols_filtered": True}, 0)
        elif path_score:
            self.matched_files += 1
            self._keep(path_score, record, 0)
        if record.get("symbols_unavailable"):
            # A failed parse cannot establish that no declaration matches.
            self.truncated = True

    def _keep(self, score, record, matches):
        worst = dict(record)
        if "symbols" in worst:
            worst["symbols_truncated"] = True
        size = self.measure(worst) + 2
        if size > self.budget:
            self.truncated = True
            return
        heapq.heappush(self.heap, Candidate(score, record, matches, size))
        self.size += size
        while self.size > self.budget:
            self.size -= heapq.heappop(self.heap).size
            self.truncated = True

    def result(self, incomplete, check_cancelled):
        check_cancelled()
        grouped, totals = {}, {}
        for candidate in sorted(self.heap, key=lambda item: (-item.score, item.record["path"], item.line)):
            check_cancelled()
            path = candidate.record["path"]
            if path not in grouped:
                grouped[path] = {**candidate.record, "symbols": []} if "symbols" in candidate.record else dict(candidate.record)
                totals[path] = candidate.matches
            grouped[path].get("symbols", []).extend(candidate.record.get("symbols", []))
        for path, record in grouped.items():
            if len(record.get("symbols", [])) < totals[path]:
                record["symbols_truncated"] = True
        return {"files": list(grouped.values()), "truncated": self.truncated or incomplete,
                "matched_files": self.matched_files, **self.extra}
