"""Response models of GET /api/v1/inventory/*.

They mirror the dicts ``app.services.inventory_query`` returns, field for field, and double as the
OpenAPI description. The authoritative contract (TypeScript interfaces, examples, error cases) is
``Documentation/Inventory/05-P1-API-CONTRACT.md``; the golden files under
``dashboard/src/pages/inventory/fixtures`` are produced from these models, so a field added here
shows up there. Timestamps are UTC ISO-8601 strings with a trailing Z. A null is always present
(never omitted), so a client can tell "unknown" from "not in this version of the API".
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ScopeInfo(BaseModel):
    restricted: bool
    tags: list[str]


# ── Assets ───────────────────────────────────────────────────────────

class AssetRow(BaseModel):
    key: str
    kind: str
    id: str
    name: str
    ip: Optional[str] = None
    asset_type: Optional[str] = None
    status: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    os_family: str
    os_product: Optional[str] = None
    os_release: Optional[str] = None
    os_version: Optional[str] = None
    os_build: Optional[str] = None
    os_edition: Optional[str] = None
    os_source: Optional[str] = None
    hw_source: Optional[str] = None
    site: Optional[str] = None
    tags: list[str]
    sources: list[str]
    last_seen: Optional[str] = None
    inventory_at: Optional[str] = None
    inventory_age_s: Optional[int] = None
    freshness: str
    software_truncated: bool


class AssetList(BaseModel):
    generated_at: str
    items: list[AssetRow]
    total: int
    page: int
    page_size: int
    sort: str
    order: str


class FacetItem(BaseModel):
    value: str
    label: str
    count: int


class FacetGroup(BaseModel):
    distinct: int
    items: list[FacetItem]


class Facets(BaseModel):
    kind: FacetGroup
    os_family: FacetGroup
    os_product: FacetGroup
    vendor: FacetGroup
    model: FacetGroup
    site: FacetGroup
    tag: FacetGroup
    source: FacetGroup
    freshness: FacetGroup
    health: FacetGroup


class FacetsResponse(BaseModel):
    generated_at: str
    total: int
    facets: Facets


# ── Change events (shared by /changes and the flyout) ────────────────

class PairInfo(BaseModel):
    event_id: int
    label: Optional[str] = None


class ChangeRow(BaseModel):
    id: int
    observed_at: str
    asset_key: str
    asset_kind: str
    asset_id: str
    asset_name: Optional[str] = None
    asset_present: bool
    server_id: Optional[str] = None
    category: str
    action: str
    subject: str
    subject_key: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    label: Optional[str] = None
    pair: Optional[PairInfo] = None
    marker: bool
    snapshot_truncated: bool
    detail: dict[str, Any]


class SoftwareStats(BaseModel):
    added: int
    removed: int
    changed: int
    upgraded: int
    downgraded: int
    paired: dict[str, int]


class ChangeStats(BaseModel):
    total: int
    software: SoftwareStats
    os: int
    firmware: int
    hardware: int


class SeriesPoint(BaseModel):
    day: str
    software: int
    os: int
    firmware: int
    hardware: int
    total: int


class ChangeWindow(BaseModel):
    since: str
    until: Optional[str] = None
    tz: str
    # true when the requested zone could not be opened on this host and the series was cut in UTC (tz says which)
    tz_fallback: bool = False


class ChangesResponse(BaseModel):
    generated_at: str
    window: ChangeWindow
    items: list[ChangeRow]
    total: int
    page: int
    page_size: int
    stats: ChangeStats
    series: list[SeriesPoint]
    # when change capture began on this installation (install-wide; null only if it was never recorded), and how many
    # days events are kept: changes older than that are removed, changes before history_start were never recorded
    history_start: Optional[str] = None
    retention_days: int


# ── Asset detail (flyout) ────────────────────────────────────────────

class IdentityValue(BaseModel):
    field: str
    label: str
    value: Optional[str] = None
    source: Optional[str] = None
    source_detail: Optional[str] = None
    observed_at: Optional[str] = None
    age_s: Optional[int] = None


class ServerSoftwareItem(BaseModel):
    package_name: str
    product_key: Optional[str] = None
    product: str
    version: Optional[str] = None
    publisher: Optional[str] = None
    category: Optional[str] = None
    architecture: Optional[str] = None
    source: Optional[str] = None
    install_date: Optional[str] = None
    first_seen_at: Optional[str] = None


class SecurityAgent(BaseModel):
    """Endpoint protection of one server. ``state`` is the answer of the Assets filter ``security_agent``; ``present``
    is the same as a nullable bool (null = unknown) for older readers; ``reason`` says why it is unknown
    (no_software_list, list_truncated, normalising or no_service_list: ``inventory_query.SECURITY_UNKNOWN_REASONS``);
    ``products`` are the endpoint-protection software titles and ``services`` the running Defender services that count
    when no title does."""
    present: Optional[bool] = None
    state: str = "unknown"
    reason: Optional[str] = None
    products: list[str]
    services: list[str] = []


class ServerSoftware(BaseModel):
    total: int
    shown: int
    hidden: int
    truncated: bool
    top: list[ServerSoftwareItem]
    security_agent: SecurityAgent


class ServerBody(BaseModel):
    os_name: Optional[str] = None
    architecture: Optional[str] = None
    hostname: Optional[str] = None
    fqdn: Optional[str] = None
    cpu_model: Optional[str] = None
    cpu_cores: Optional[int] = None
    cpu_physical_cores: Optional[int] = None
    memory_total_bytes: Optional[int] = None
    disks: list[dict[str, Any]]
    virtualization: Optional[str] = None
    bios_version: Optional[str] = None
    bios_date: Optional[str] = None
    boot_time: Optional[str] = None
    baselined: bool
    software: ServerSoftware


class ModuleItem(BaseModel):
    ent_index: int
    name: Optional[str] = None
    serial: Optional[str] = None
    hw_revision: Optional[str] = None
    fw_revision: Optional[str] = None


class DeviceModuleGroup(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    class_: str = Field(alias="class")
    class_label: str
    model: Optional[str] = None
    items: list[ModuleItem]
    count: int


class HaInfo(BaseModel):
    mode: Optional[str] = None
    role: Optional[str] = None
    peer: Optional[str] = None
    sync: Optional[str] = None
    observed_at: Optional[str] = None
    age_s: Optional[int] = None


class DeviceBody(BaseModel):
    profile: Optional[str] = None
    group: Optional[str] = None
    device_type: Optional[str] = None
    sys_object_id: Optional[str] = None
    controller_managed: bool
    modules: list[DeviceModuleGroup]
    modules_truncated: bool
    ha: Optional[HaInfo] = None


class DetailChanges(BaseModel):
    items: list[ChangeRow]
    total: int
    # the oldest recorded event of the asset (and its SNMP twin), null when there is none: the Changes page opened
    # with since=first_change_at and this asset lists exactly `total` events
    first_change_at: Optional[str] = None


class DetailLinks(BaseModel):
    server_id: Optional[str] = None
    device_id: Optional[str] = None


class AssetDetail(BaseModel):
    generated_at: str
    asset: AssetRow
    identity: list[IdentityValue]
    raw: dict[str, Optional[str]]
    server: Optional[ServerBody] = None
    device: Optional[DeviceBody] = None
    changes: DetailChanges
    links: DetailLinks


# ── Summary ──────────────────────────────────────────────────────────

class SummaryAssets(BaseModel):
    total: int
    server: int
    device: int
    linked_devices: int


class SummaryCoverage(BaseModel):
    fresh: int
    aging: int
    stale: int
    never: int
    # assets that cannot report an inventory by design (ping-only devices, controller-managed children, servers in a
    # collection mode with no inventory upload); fresh_pct is over the others (total - not_applicable)
    not_applicable: int = 0
    fresh_pct: Optional[float] = None
    fresh_hours: dict[str, int]
    stale_days: int


class SummaryOsRow(BaseModel):
    family: str
    product: Optional[str] = None
    release: Optional[str] = None
    count: int


class SummaryOs(BaseModel):
    families: int
    releases: int
    unknown: int
    top: list[SummaryOsRow]


class SummaryProduct(BaseModel):
    product_key: str
    name: str
    category: Optional[str] = None
    hosts: int
    versions: int


class SecurityCoverage(BaseModel):
    """Endpoint protection over all servers: covered + missing + unknown = total (the number of servers)."""
    covered: int
    missing: int = 0
    unknown: int = 0
    total: int


class SprawlBasis(BaseModel):
    """What the version-sprawl ranking was computed over. ``all_products``: every product (exact). ``top_installed``: the
    ``candidates`` most installed of ``products`` products (a product with few installs cannot be shown; the panel says
    so). The host ranking (``top``) is exact either way."""
    scope: str
    candidates: int
    products: int


class SummarySoftware(BaseModel):
    products: int
    installs: int
    publishers: int
    hidden_categories: list[str]
    top: list[SummaryProduct]
    sprawl: list[SummaryProduct]
    sprawl_basis: SprawlBasis = SprawlBasis(scope="all_products", candidates=0, products=0)
    security_agent_coverage: SecurityCoverage


class SummaryModel(BaseModel):
    vendor: str
    model: str
    count: int


class SummaryHardware(BaseModel):
    devices: int
    vendors: int
    models: int
    top_models: list[SummaryModel]


class SummaryChangeSoftware(BaseModel):
    added: int
    removed: int
    changed: int
    upgraded: int
    downgraded: int


class SummaryChanges(BaseModel):
    since: str
    total: int
    software: SummaryChangeSoftware
    os: int
    firmware: int
    hardware: int


class NotCollected(BaseModel):
    count: int


class SummaryHealth(BaseModel):
    missing_os_version: int
    missing_model: int
    missing_serial: int
    unknown_vendor: int
    stale: int
    never: int
    software_truncated: int
    missing_security_agent: int
    # assets that cannot report by design (drill: freshness=not_applicable); the identity lines above skip them
    not_collected_by_design: NotCollected = NotCollected(count=0)


class DataStatus(BaseModel):
    """How much of the data is still being normalised. While ``normalising`` is true the figures that depend on it
    (software, endpoint protection, OS) are incomplete and the pages say so."""
    normalising: bool
    pending_software_rows: int
    pending_identity_rows: int


class SummaryResponse(BaseModel):
    generated_at: str
    scope: ScopeInfo
    assets: SummaryAssets
    coverage: SummaryCoverage
    os: SummaryOs
    software: SummarySoftware
    hardware: SummaryHardware
    changes_7d: SummaryChanges
    health: SummaryHealth
    data_status: DataStatus
    lifecycle: Optional[dict[str, Any]] = None


# ── Operating systems ────────────────────────────────────────────────

class OsVersion(BaseModel):
    version: Optional[str] = None
    count: int
    raw_example: Optional[str] = None


class OsCurrency(BaseModel):
    latest: int
    previous: int
    older: int
    unknown: int
    latest_version: str
    previous_version: Optional[str] = None


class OsRelease(BaseModel):
    release: Optional[str] = None
    count: int
    latest_version: Optional[str] = None
    distinct_versions: int
    currency: Optional[OsCurrency] = None
    versions: list[OsVersion]


class OsProduct(BaseModel):
    product: Optional[str] = None
    count: int
    releases: list[OsRelease]


class OsFamily(BaseModel):
    family: str
    label: str
    count: int
    products: list[OsProduct]


class OsResponse(BaseModel):
    generated_at: str
    scope: ScopeInfo
    kind: Optional[str] = None
    total: int
    families: list[OsFamily]


# ── Hardware ─────────────────────────────────────────────────────────

class HwVendorRow(BaseModel):
    vendor: Optional[str] = None
    count: int
    models: int                       # known models (the same number as models_known)
    models_known: int = 0
    unknown_model_devices: int = 0    # devices of this vendor that report no model
    firmware_versions: int
    serial_missing: int               # = the Assets list health=missing_serial for this vendor
    not_collected: int = 0            # devices that cannot report by design (not in serial_missing or the percentage)
    serial_coverage_pct: Optional[float] = None    # over the devices that can report; null when none can
    modules: int


class HwModelRow(BaseModel):
    vendor: str
    model: Optional[str] = None
    unknown: bool = False             # true for the row of devices that report no model (model is null)
    count: int
    firmware_versions: int
    serial_missing: int
    not_collected: int = 0
    serial_coverage_pct: Optional[float] = None
    modules: int


class MatrixCell(BaseModel):
    release: str
    count: int


class MatrixRow(BaseModel):
    model: Optional[str] = None
    unknown: bool = False
    count: int
    cells: list[MatrixCell]


class HwMatrix(BaseModel):
    vendor: str
    releases: list[str]
    models: list[MatrixRow]


class HwModuleGroup(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    class_: str = Field(alias="class")
    class_label: str
    model: Optional[str] = None
    count: int
    devices: int
    with_serial: int


class HwNetwork(BaseModel):
    devices: int
    models_known: int = 0             # distinct (vendor, model) pairs with both known: the Overview's model figure
    unknown_model_devices: int = 0    # devices with no model, whatever their vendor
    vendors: list[HwVendorRow]
    models: list[HwModelRow]


class DistItem(BaseModel):
    value: str
    label: str
    count: int


class CpuMore(BaseModel):
    models: int
    servers: int


class HwServers(BaseModel):
    total: int
    cpu_models: list[DistItem]
    cpu_models_more: CpuMore
    memory: list[DistItem]
    disks: list[DistItem]
    virtualization: Optional[list[DistItem]] = None


class HardwareResponse(BaseModel):
    generated_at: str
    scope: ScopeInfo
    network: HwNetwork
    matrix: Optional[HwMatrix] = None
    modules: list[HwModuleGroup]
    servers: HwServers


# ── Software catalog (GET /inventory/software*) ──────────────────────

class VersionBarItem(BaseModel):
    version: Optional[str] = None
    hosts: int
    scheme: str
    rank: Optional[int] = None


class VersionBarOther(BaseModel):
    versions: int
    hosts: int


class VersionBar(BaseModel):
    top: list[VersionBarItem]
    other: VersionBarOther


class CatalogRow(BaseModel):
    product_key: str
    name: str
    publisher: Optional[str] = None
    publisher_key: Optional[str] = None
    category: str
    norm_status: str
    hosts: int
    installs: int
    version_count: int
    latest_version: Optional[str] = None
    distribution: VersionBar
    first_seen: Optional[str] = None
    last_change: Optional[str] = None


class CatalogResponse(BaseModel):
    generated_at: str
    scope: ScopeInfo
    items: list[CatalogRow]
    total: int
    page: int
    page_size: int
    sort: str
    order: str
    hidden_products: int
    hidden_installs: int
    unmatched_titles: int
    hidden_categories: list[str]
    data_status: DataStatus


class PublisherItem(BaseModel):
    value: str
    label: str
    products: int
    installs: int
    hosts: int


class PublishersResponse(BaseModel):
    generated_at: str
    scope: ScopeInfo
    total: int
    distinct: int
    items: list[PublisherItem]
    data_status: DataStatus


class ProductHeader(BaseModel):
    product_key: str
    name: str
    publisher: Optional[str] = None
    publisher_key: Optional[str] = None
    category: str
    norm_status: str
    rule_id: Optional[str] = None
    hosts: int
    installs: int
    version_count: int
    latest_version: Optional[str] = None
    first_seen: Optional[str] = None
    last_change: Optional[str] = None
    mixed_schemes: bool


class ProductVersion(BaseModel):
    version: str
    hosts: int
    installs: int
    scheme: str
    rank: Optional[int] = None
    generation: Optional[str] = None


class Unversioned(BaseModel):
    """The rows that report no version (NULL or blank). Not a version: ``address`` is the ``version`` value that lists them
    in ``/software/<key>/installations`` (a version spelled "unknown" is an ordinary version)."""
    hosts: int
    installs: int
    address: str = "__none__"


class EvidenceItem(BaseModel):
    package_name: str
    vendor: Optional[str] = None
    source: Optional[str] = None
    hosts: int
    installs: int
    status: Optional[str] = None
    rule_id: Optional[str] = None


class Evidence(BaseModel):
    items: list[EvidenceItem]
    total: int
    truncated: bool


class ProductResponse(BaseModel):
    generated_at: str
    scope: ScopeInfo
    header: ProductHeader
    versions: list[ProductVersion]
    unversioned: Unversioned
    evidence: Evidence
    data_status: DataStatus


class InstallationRow(BaseModel):
    asset_key: str
    asset_id: str
    asset_name: str
    package_name: str
    version: Optional[str] = None
    architecture: Optional[str] = None
    source: Optional[str] = None
    install_date: Optional[str] = None
    first_seen_at: Optional[str] = None
    updated_at: Optional[str] = None


class InstallationsResponse(BaseModel):
    generated_at: str
    product_key: str
    version: Optional[str] = None
    items: list[InstallationRow]
    total: int
    page: int
    page_size: int
    sort: str
    order: str


# ── Trends and settings ──────────────────────────────────────────────

class TrendPoint(BaseModel):
    day: str
    value: int


class TrendSeries(BaseModel):
    dimension: str
    points: list[TrendPoint]


class TrendWindow(BaseModel):
    since: str
    until: str


class TrendsResponse(BaseModel):
    generated_at: str
    restricted: bool
    enabled: bool
    metric: str
    dimension: Optional[str] = None
    days: int
    window: TrendWindow
    first_day: Optional[str] = None
    days_with_data: int
    dimensions_total: int
    truncated: bool
    series: list[TrendSeries]


class FreshHours(BaseModel):
    server: int
    device: int


class InventorySettings(BaseModel):
    fresh_hours: FreshHours
    stale_days: int
    change_retention_days: int
    snapshot_hour: int
    hide_categories_by_default: list[str]
    trends_enabled: bool


class SettingsLimit(BaseModel):
    min: int
    max: int


class SettingsLimits(BaseModel):
    fresh_hours: SettingsLimit
    stale_days: SettingsLimit
    change_retention_days: SettingsLimit
    snapshot_hour: SettingsLimit


class SettingsResponse(BaseModel):
    settings: InventorySettings
    defaults: InventorySettings
    limits: SettingsLimits
    categories: list[str]
    updated_at: Optional[str] = None
    changed: list[str]
    adjusted: list[str]
