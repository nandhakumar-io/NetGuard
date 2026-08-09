from app.core.database import Base  # noqa: F401
from app.models.alert import Alert, AlertSeverity, AlertSource  # noqa: F401
from app.models.alert_rule import (  # noqa: F401
    AlertRule,
    AlertRuleMetric,
    AlertRuleOperator,
)
from app.models.alert_snooze import AlertSnooze  # noqa: F401
from app.models.audit_log import AuditLog  # noqa: F401
from app.models.change_request import (  # noqa: F401
    ChangePriority,
    ChangeRequest,
    ChangeStatus,
)
from app.models.compliance_baseline import ComplianceBaseline  # noqa: F401
from app.models.config_drift import (  # noqa: F401
    ConfigDrift,
    DriftBaseline,
    DriftSeverity,
    DriftStatus,
)
from app.models.config_template import (  # noqa: F401
    ConfigTemplate,
    ConfigTemplateVersion,
)
from app.models.deployment import (  # noqa: F401
    Deployment,
    DeploymentStatus,
    HealthCheckResult,
)
from app.models.device import (  # noqa: F401
    Device,
    DeviceLifecycleState,
    DeviceStatus,
    DeviceVendor,
    SnmpVersion,
)
from app.models.device_group import DeviceGroup  # noqa: F401
from app.models.device_metric import DeviceMetric, HealthColor  # noqa: F401
from app.models.discovered_neighbor import DiscoveredNeighbor  # noqa: F401
from app.models.firmware_upgrade import (  # noqa: F401
    FirmwareUpgrade,
    FirmwareUpgradeStatus,
)
from app.models.golden_config import GoldenConfig  # noqa: F401
from app.models.interface_status import (  # noqa: F401
    InterfaceOperStatus,
    InterfaceStatus,
)
from app.models.maintenance_window import (  # noqa: F401
    MaintenanceScope,
    MaintenanceWindow,
)
from app.models.notification import (  # noqa: F401
    Notification,
    NotificationEventType,
    NotificationSeverity,
)
from app.models.path_trace import (  # noqa: F401
    HopStatus,
    PathHop,
    PathTrace,
    PathTraceStatus,
)
from app.models.protocol_operation import ProtocolName, ProtocolOperation  # noqa: F401
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.snapshot import ConfigSnapshot  # noqa: F401
from app.models.syslog_message import SyslogMessage, SyslogSeverity  # noqa: F401
from app.models.user import User, UserRole  # noqa: F401
from app.models.webhook import WebhookEndpoint, WebhookType  # noqa: F401
