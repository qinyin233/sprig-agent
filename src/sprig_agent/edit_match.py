"""Bounded location hints for a rejected exact edit; never choose a replacement."""

import io


def line_number(text, position):
    # Physical CR/LF lines, including lone CR; a CRLF is one boundary.
    number = 1 + text.count("\n", 0, position) + text.count("\r", 0, position) - text.count("\r\n", 0, position)
    if 0 < position < len(text) and text[position] == "\n" and text[position - 1] == "\r":
        number -= 1
    return number


def failure_details(before, previous, index, starts, check_cancelled):
    """Inputs already passed file privacy/size checks. Return metadata only.

    Exact matching remains unchanged. Read suggestions use at most three short
    stripped-line anchors from the first 64 old lines, without similarity scores
    or replacement text. Trimming is for navigation, never for applying edits.
    """
    check_cancelled()
    reason = "ambiguous" if starts else "not_found" if previous else "empty_old"
    result = {"index": index, "reason": reason, "exact_matches_min": 2 if starts else 0,
              "read_suggestions": [], "suggestions_truncated": False,
              "note": "Navigation hints only. Read current lines and submit a unique exact old block; no edit was applied."}
    size = min(50, line_number(previous, len(previous)) + 4)
    offsets = set()
    def suggest(number):
        offset = max(1, number - 2)
        if offset in offsets:
            return True
        if len(offsets) == 3:
            result["suggestions_truncated"] = True
            return False
        offsets.add(offset)
        result["read_suggestions"].append({"offset": offset, "limit": size, "line_numbers": False})
        return True
    if starts:
        for start in starts:
            check_cancelled()
            suggest(line_number(before, start))
        return result
    if not previous:
        return result
    # Diagnose newline-only differences, but do not normalize a write or infer
    # that a normalized occurrence is a unique exact replacement.
    normalized = before.replace("\r\n", "\n").replace("\r", "\n")
    needle = previous.replace("\r\n", "\n").replace("\r", "\n")
    check_cancelled()
    first = normalized.find(needle)
    if first >= 0:
        result["reason"] = "line_endings_mismatch"
        result["normalized_matches_min"] = 1 if normalized.find(needle, first + 1) < 0 else 2
        # Normalizing CR/LF preserves physical line numbers.
        suggest(line_number(normalized, first))
    anchors = set()
    for number, line in enumerate(io.StringIO(previous, newline=""), 1):
        check_cancelled()
        if number > 64:
            break
        anchor = line.strip()
        if 0 < len(anchor) <= 256:
            anchors.add(anchor)
        if len(anchors) == 3:
            break
    if anchors:
        for number, line in enumerate(io.StringIO(before, newline=""), 1):
            check_cancelled()
            if line.strip() in anchors and not suggest(number):
                break
    check_cancelled()
    return result
