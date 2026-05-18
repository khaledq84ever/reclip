"""Native per-site extractors used by reclip's host-aware router.

Each module exposes:
    info(url)               -> (info_dict, error_str)
    download(url, fmt, dest_dir, on_progress=None) -> (file_path, filename, error_str)

info_dict has the keys reclip's /api/info returns:
    {title, thumbnail, duration, uploader, formats}
"""
