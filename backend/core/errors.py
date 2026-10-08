"""错误码与异常。

纯函数层：不依赖 flask，也不依赖主程序模块，便于独立单测。
错误码与设计文档 §9.1 的表格一一对应。
"""


class AiAssistantError(Exception):
    """插件统一异常：携带错误码、HTTP 状态与结构化细节。

    路由层把它映射为 `{success: false, msg, error, detail}`（与主程序插件
    接口的统一响应格式一致，见 `core/plugins/routes.py:31-45`）。
    """

    def __init__(self, code: str, message: str = '', *, http_status: int = 400, detail=None):
        super().__init__(message or code)
        self.code = str(code or 'plugin_error')
        self.message = str(message or '')
        self.http_status = int(http_status)
        self.detail = detail if detail is not None else {}

    def to_dict(self) -> dict:
        payload = {
            'success': False,
            'msg': self.message or self.code,
            'error': self.code,
        }
        if self.detail:
            payload['detail'] = self.detail
        return payload


class CancelledError(AiAssistantError):
    """协作式取消：用户取消或作业被暂停。"""

    def __init__(self, message: str = '已取消'):
        super().__init__(CODE_LLM_CANCELLED, message, http_status=409)


# ---- 配置与档案 ----
CODE_SECRET_MISSING = 'secret_missing'
CODE_PROVIDER_NOT_FOUND = 'provider_not_found'
CODE_PROFILE_NOT_FOUND = 'profile_not_found'
CODE_PROFILE_INVALID = 'profile_invalid'
CODE_UNKNOWN_VARIABLE = 'unknown_variable'
CODE_MISSING_VARIABLE = 'missing_variable'
CODE_INVALID_PAYLOAD = 'invalid_payload'
CODE_SSRF_BLOCKED = 'ssrf_blocked'

# ---- LLM ----
CODE_LLM_AUTH_FAILED = 'llm_auth_failed'
CODE_LLM_RATE_LIMITED = 'llm_rate_limited'
CODE_LLM_TIMEOUT = 'llm_timeout'
CODE_LLM_BAD_REQUEST = 'llm_bad_request'
CODE_LLM_BAD_RESPONSE = 'llm_bad_response'
CODE_LLM_CANCELLED = 'llm_cancelled'

# ---- 结果 ----
CODE_PARSE_FAILED = 'parse_failed'
CODE_VALIDATION_FAILED = 'validation_failed'

# ---- 数据 ----
CODE_CARD_UNREADABLE = 'card_unreadable'
CODE_REVISION_CONFLICT = 'revision_conflict'
CODE_WRITE_FAILED = 'write_failed'
CODE_QUOTA_EXCEEDED = 'quota_exceeded'
CODE_JOB_ALREADY_RUNNING = 'job_already_running'
CODE_JOB_NOT_FOUND = 'job_not_found'
CODE_APPLY_STALE = 'apply_stale'

ALL_CODES = frozenset({
    CODE_SECRET_MISSING, CODE_PROVIDER_NOT_FOUND, CODE_PROFILE_NOT_FOUND,
    CODE_PROFILE_INVALID, CODE_UNKNOWN_VARIABLE, CODE_MISSING_VARIABLE,
    CODE_INVALID_PAYLOAD, CODE_SSRF_BLOCKED,
    CODE_LLM_AUTH_FAILED, CODE_LLM_RATE_LIMITED, CODE_LLM_TIMEOUT,
    CODE_LLM_BAD_REQUEST, CODE_LLM_BAD_RESPONSE, CODE_LLM_CANCELLED,
    CODE_PARSE_FAILED, CODE_VALIDATION_FAILED,
    CODE_CARD_UNREADABLE, CODE_REVISION_CONFLICT, CODE_WRITE_FAILED,
    CODE_QUOTA_EXCEEDED, CODE_JOB_ALREADY_RUNNING, CODE_JOB_NOT_FOUND,
    CODE_APPLY_STALE,
})
