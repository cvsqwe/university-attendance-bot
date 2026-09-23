from maxapi.router import Router

from .admin import router as admin_router
from .schedule import router as schedule_router
from .student import router as student_router


def get_root_router() -> Router:
    root = Router(name="root")
    root.include_router(admin_router)
    root.include_router(student_router)
    root.include_router(schedule_router)
    return root
