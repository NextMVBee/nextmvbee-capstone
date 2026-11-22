"""Record identity. Ids are unique across the whole database: "<project_id>.<PREFIX>-<nnn>".
The part after the dot is the display id shown to the founder (REQ-001, DEC-003). Two projects may both show DEC-001
without colliding."""
import re
from app.core.sqlguard import require_guarded
PREFIXES = ("REQ", "ASM", "Q", "SRC", "CLM", "DEC", "CMP", "TSK", "ACC", "ENT", "IFC", "REL", "VCK")   # ENT, IFC, REL, VCK: entities, interfaces, component relations, verification checks
PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.(REQ|ASM|Q|SRC|CLM|DEC|CMP|TSK|ACC|ENT|IFC|REL|VCK)-\d{3,}$")


def make_id(project_id: str, prefix: str, n: int) -> str:
    assert prefix in PREFIXES and "." not in project_id
    return f"{project_id}.{prefix}-{n:03d}"


def display_id(record_id: str) -> str:
    return record_id.split(".", 1)[1] if "." in record_id else record_id


def prefix_of(record_id: str) -> str:
    return display_id(record_id).split("-", 1)[0]


def next_number(conn, table: str, project_id: str, prefix: str) -> int:
    require_guarded(conn)
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=? AND id LIKE ?", (project_id, f"{project_id}.{prefix}-%")).fetchone()
    return int(row[0]) + 1
