"""Reading the secrets status report back: the cells of its tables and the findings of one rule."""


def cells_of(table: str) -> list[list[str]]:
    """The body rows of a Markdown table, cells stripped."""
    return [[cell.strip() for cell in line.strip("|").split(" | ")] for line in table.strip().splitlines()[2:]]


def findings_of(output: str, rule: str) -> dict[str, list[str]]:
    """The findings of one rule, keyed by subject: [detail, recommendation]."""
    table = output.split("## Findings\n\n", 1)[1].split("\n\n")[0] if "## Findings" in output else ""
    return {cells[1]: cells[2:] for cells in cells_of(table) if cells[0] == rule}
