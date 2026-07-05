class WorkupAgentError(Exception):
    """Base error for controlled task termination."""


class InputValidationError(WorkupAgentError):
    pass


class CompanyResolutionError(WorkupAgentError):
    pass


class DataSourceError(WorkupAgentError):
    pass


class TemplateMappingError(WorkupAgentError):
    pass

