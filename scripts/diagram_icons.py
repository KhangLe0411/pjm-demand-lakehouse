"""Simplified vendor marks, drawn in brand colours.

These are geometric approximations, not official logo files. Official assets are not
redistributed here: dropping a licensed SVG into docs/diagrams/icons/ and pointing the
matching entry at it is a one-line change, and draw.io's own Azure/AWS/GCP stencil
libraries carry properly licensed icons if you would rather use those.

Each mark is a self-contained 24x24 SVG so it embeds as a data URI and needs no network
fetch in either output format.
"""
from __future__ import annotations

import base64

ICONS: dict[str, str] = {
    # Databricks: stacked angled bands, brand red
    "databricks": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M2 15.4 12 21 22 15.4v2.4L12 23.4 2 17.8z" fill="#FF3621"/>'
        '<path d="M2 11.2 12 16.8 22 11.2v2.4L12 19.2 2 13.6z" fill="#FF3621" opacity=".75"/>'
        '<path d="M2 7 12 12.6 22 7v2.4L12 15 2 9.4z" fill="#FF3621" opacity=".5"/>'
        '<path d="M12 1 22 6.6 12 12.2 2 6.6z" fill="#FF3621"/></svg>',

    # Delta Lake: delta triangle over a lake band
    "delta": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M12 2 22 19H2z" fill="#00ADD4"/>'
        '<path d="M4.6 15h14.8l1.2 2H3.4z" fill="#003D4D" opacity=".55"/>'
        '<path d="M2 20.4c3-1.5 5-1.5 8 0s5 1.5 8 0v2c-3 1.5-5 1.5-8 0s-5-1.5-8 0z"'
        ' fill="#00ADD4" opacity=".7"/></svg>',

    # Parquet: columnar storage, columns of differing height
    "parquet": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect x="2" y="8" width="4" height="13" rx="1" fill="#0F6CBD"/>'
        '<rect x="7.5" y="4" width="4" height="17" rx="1" fill="#2B88D8"/>'
        '<rect x="13" y="10" width="4" height="11" rx="1" fill="#0F6CBD"/>'
        '<rect x="18.5" y="6" width="3.5" height="15" rx="1" fill="#2B88D8"/></svg>',

    # ADLS Gen2 / Azure Storage
    "adls": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M3 6h18v4H3z" fill="#0078D4"/><path d="M3 10h18v4H3z" fill="#50B0E8"/>'
        '<path d="M3 14h18v4H3z" fill="#0078D4" opacity=".8"/>'
        '<circle cx="18.5" cy="8" r="1" fill="#fff"/>'
        '<circle cx="18.5" cy="12" r="1" fill="#fff"/>'
        '<circle cx="18.5" cy="16" r="1" fill="#fff"/></svg>',

    # Azure chevron
    "azure": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M9.6 2.5 3 18.2l4.6.1L14.6 2.5z" fill="#0078D4"/>'
        '<path d="M12.4 6.4 21.5 21.5H6.9l6-2.2-3.3-4z" fill="#0078D4" opacity=".7"/></svg>',

    # Power BI: rising bars, brand yellow
    "powerbi": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect x="3" y="13" width="4.6" height="9" rx="1" fill="#F2C811"/>'
        '<rect x="9.7" y="7" width="4.6" height="15" rx="1" fill="#F2C811" opacity=".85"/>'
        '<rect x="16.4" y="2" width="4.6" height="20" rx="1" fill="#F2C811"/></svg>',

    # Python
    "python": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M11.9 2c-2.6 0-4.4.9-4.4 3v2.2h4.6v.8H5.6C3.6 8 2 9.4 2 12.2s1.4 4 3.4 4h1.6'
        'v-2.6c0-2 1.6-3.4 3.6-3.4h4.2c1.7 0 3-1.3 3-3V5c0-1.9-1.7-3-3.9-3z" fill="#3776AB"/>'
        '<path d="M12.1 22c2.6 0 4.4-.9 4.4-3v-2.2h-4.6v-.8h6.5c2 0 3.6-1.4 3.6-4.2s-1.4-4-3.4-4'
        'h-1.6v2.6c0 2-1.6 3.4-3.6 3.4H9.2c-1.7 0-3 1.3-3 3V19c0 1.9 1.7 3 3.9 3z" fill="#FFD43B"/>'
        '</svg>',

    # REST API endpoint
    "api": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<circle cx="12" cy="12" r="9.2" fill="none" stroke="#5B6770" stroke-width="1.8"/>'
        '<ellipse cx="12" cy="12" rx="4" ry="9.2" fill="none" stroke="#5B6770" stroke-width="1.4"/>'
        '<path d="M3 9.4h18M3 14.6h18" stroke="#5B6770" stroke-width="1.4"/></svg>',

    # Weather forecast
    "weather": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<circle cx="8.5" cy="8" r="4" fill="#FDB813"/>'
        '<path d="M8 19c-2.8 0-5-2-5-4.4S5.2 10 8 10c.8-2.4 3-4 5.6-4 3.4 0 6.1 2.5 6.1 5.6'
        'l-.1.8c1.4.5 2.4 1.8 2.4 3.3 0 2-1.7 3.3-3.8 3.3z" fill="#C7D5E0"/></svg>',

    # Gradient-boosted trees
    "model": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<circle cx="12" cy="4" r="2.6" fill="#337AB7"/>'
        '<circle cx="6" cy="13" r="2.6" fill="#5BA3D0"/>'
        '<circle cx="18" cy="13" r="2.6" fill="#5BA3D0"/>'
        '<circle cx="3.4" cy="21" r="2.2" fill="#9FC7E3"/>'
        '<circle cx="9" cy="21" r="2.2" fill="#9FC7E3"/>'
        '<circle cx="20.6" cy="21" r="2.2" fill="#9FC7E3"/>'
        '<path d="M12 6.6 6 10.4M12 6.6l6 3.8M6 15.6l-2.6 3.2M6 15.6 9 18.8M18 15.6l2.6 3.2"'
        ' stroke="#337AB7" stroke-width="1.3" fill="none"/></svg>',

    # Quarantine / blocked
    "quarantine": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<circle cx="12" cy="12" r="9.4" fill="none" stroke="#B85450" stroke-width="2.2"/>'
        '<path d="M5.6 5.6 18.4 18.4" stroke="#B85450" stroke-width="2.2"/></svg>',

    # Event / stream (for the excluded Kafka box)
    "stream": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<circle cx="6" cy="6" r="2.4" fill="#7F8C8D"/><circle cx="6" cy="18" r="2.4" fill="#7F8C8D"/>'
        '<circle cx="18" cy="12" r="2.4" fill="#7F8C8D"/>'
        '<path d="M8.2 7 15.8 11M8.2 17l7.6-4" stroke="#7F8C8D" stroke-width="1.6"/></svg>',

    # MLflow: simplified tricolour mark
    "mlflow": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path d="M12 2 2.4 19.5h6.2L12 13l3.4 6.5h6.2z" fill="#0194E2"/>'
        '<circle cx="12" cy="7.6" r="2.4" fill="#43C9ED"/></svg>',

    # Warehouse (excluded)
    "warehouse": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<ellipse cx="12" cy="5.5" rx="8.4" ry="3.2" fill="#8FA8B8"/>'
        '<path d="M3.6 5.5v13c0 1.8 3.8 3.2 8.4 3.2s8.4-1.4 8.4-3.2v-13" fill="#B9CBD6"/>'
        '<ellipse cx="12" cy="18.5" rx="8.4" ry="3.2" fill="#8FA8B8" opacity=".55"/></svg>',
}


def data_uri(name: str) -> str:
    """draw.io form: data:image/svg+xml,<base64> - comma, then raw base64."""
    b = base64.b64encode(ICONS[name].encode()).decode()
    return f"data:image/svg+xml,{b}"


def svg_href(name: str) -> str:
    """SVG <image href> form needs the ;base64 marker."""
    b = base64.b64encode(ICONS[name].encode()).decode()
    return f"data:image/svg+xml;base64,{b}"
