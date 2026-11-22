"""Simple offline publication decision for licensed exhibition images."""


def permit_render(render, license_record):
    """Return whether the rendered image fits the stored size and time window."""
    return (render['artifact'] == license_record['artifact']
            and render['width'] <= license_record['max_width']
            and license_record['start'] <= render['day'] <= license_record['end'])
