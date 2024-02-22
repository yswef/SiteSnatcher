"""Central place for every colour and size used by the interface."""

COLORS = {
    "bg":          "#0E1117",
    "sidebar":     "#12161F",
    "card":        "#171B26",
    "card_hover":  "#1D2230",
    "border":      "#232838",
    "text":        "#E6E9F0",
    "muted":       "#7C879E",
    "accent":      "#4F7CFF",
    "accent_dark": "#3B62D6",
    "ok":          "#3DD68C",
    "warn":        "#F2B84B",
    "error":       "#FF5F6D",
    "field":       "#1B2030",
}

LOG_COLORS = {
    "info":   COLORS["muted"],
    "ok":     COLORS["ok"],
    "warn":   COLORS["warn"],
    "error":  COLORS["error"],
    "accent": COLORS["accent"],
}

RADIUS = 12
PAD = 14


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
