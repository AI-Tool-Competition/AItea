class AppError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def fail(status, code, message):
    raise AppError(status, code, message)


def check(id, label, status, reason, refs=None):
    return {"id": id, "label": label, "status": status, "reason": reason, "evidence_refs": refs or []}
