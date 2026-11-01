"""Shared extraction interface: MODE_EXTRACT (scan+carve) and MODE_REPORT (scan only, plan later)."""
MODE_EXTRACT = "extract-while-scanning"
MODE_REPORT = "report-first"
MODE_ALIASES = {
    "extract-as-we-go": MODE_EXTRACT,
    "extract": MODE_EXTRACT,
    "carve": MODE_EXTRACT,
    "report": MODE_REPORT,
}

def extract_mode(name):
    text = str(name or "").strip().lower()
    if text in (MODE_EXTRACT, MODE_REPORT):
        return text
    return MODE_ALIASES.get(text, text)
