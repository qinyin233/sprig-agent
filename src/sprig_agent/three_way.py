"""Conservatively combine text changes separated by unique unchanged lines."""


def _positions(lines, check):
    positions = {}
    for index, line in enumerate(lines):
        if index % 256 == 0:
            check()
        positions[line] = None if line in positions else index
    return positions


def merge_text(base, target, current, check=lambda: None):
    """Apply base→target while retaining independent base→current changes."""
    check()
    if target == base or current == target:
        return current
    if current == base:
        return target

    base, target, current = (text.splitlines(keepends=True) for text in (base, target, current))
    source, desired, working = (_positions(lines, check) for lines in (base, target, current))
    anchors = []
    last_target = last_current = -1
    for line, index in source.items():  # In ascending order of the first occurrence.
        if index is None:
            continue
        if index % 256 == 0:
            check()
        in_target, in_current = desired.get(line), working.get(line)
        if in_target is None or in_current is None:
            continue
        if in_target <= last_target or in_current <= last_current:
            raise ValueError("common lines changed order")
        anchors.append((index, in_target, in_current))
        last_target, last_current = in_target, in_current

    merged = []
    a = b = c = 0
    # Handle each ambiguous gap as a unit, including repeated lines.
    for end_a, end_b, end_c in (*anchors, (len(base), len(target), len(current))):
        check()
        saved, inverse, live = base[a:end_a], target[b:end_b], current[c:end_c]
        if inverse == saved:
            merged.extend(live)
        elif live in (saved, inverse):
            merged.extend(inverse)
        else:
            raise ValueError(f"overlapping or ambiguous changes near saved lines {a + 1}–{max(a + 1, end_a)}")
        if end_a < len(base):
            merged.append(base[end_a])
        a, b, c = end_a + 1, end_b + 1, end_c + 1
    return "".join(merged)
