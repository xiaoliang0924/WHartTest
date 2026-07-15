from django.db import DatabaseError, connection
from django.http import JsonResponse


def database_is_available():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return False

    return True


def health_check(request):
    """Report readiness only when Django can execute a database query."""
    if not database_is_available():
        return JsonResponse(
            {"status": "unavailable", "database": "unavailable"},
            status=503,
        )

    return JsonResponse({"status": "ok", "database": "ok"})
