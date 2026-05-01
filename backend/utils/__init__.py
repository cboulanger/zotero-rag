
def format_file_size(num_bytes: int) -> str:
    """Return a human-readable file size string with 2 decimal places."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.2f} KB"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / 1024 ** 2:.2f} MB"
    return f"{num_bytes / 1024 ** 3:.2f} GB"
