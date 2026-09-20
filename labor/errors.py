"""统一的接口错误类型。"""


class ApiError(Exception):
    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        payload = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def bad_request(message="请求参数有误", details=None):
    return ApiError(400, "bad_request", message, details)


def unauthorized(message="缺少或无效的访问令牌"):
    return ApiError(401, "unauthorized", message)


def forbidden(message="无权执行该操作"):
    return ApiError(403, "forbidden", message)


def not_found(message="资源不存在"):
    return ApiError(404, "not_found", message)


def conflict(message, code="conflict", details=None):
    return ApiError(409, code, message, details)


def unprocessable(message, code="eligibility_failed", details=None):
    return ApiError(422, code, message, details)
