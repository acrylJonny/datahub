import logging
from typing import Iterable, Dict, Any, Optional, List
from dataclasses import field, dataclass
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth
from pydantic import Field, BaseModel

from datahub.configuration.common import ConfigModel
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.source import Source, SourceReport
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.emitter.mce_builder import (
    make_assertion_urn,
    make_dataset_urn_with_platform_instance,
    make_schema_field_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionResultClass,
    AssertionResultTypeClass,
    AssertionRunEventClass,
    AssertionRunStatusClass,
    AssertionTypeClass,
    DatasetAssertionInfoClass,
    DatasetAssertionScopeClass,
    FabricTypeClass,
    PartitionSpecClass,
    PartitionTypeClass,
)

logger = logging.getLogger(__name__)

def snake_to_camel(string: str) -> str:
    components = string.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

def camel_to_snake(string: str) -> str:
    return ''.join(['_' + char.lower() if char.isupper() else char for char in string]).lstrip('_')


class DynamicConversion:
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Any:
        field_dict = {}
        for fld in cls.__dataclass_fields__.values():
            camel_key = snake_to_camel(fld.name)
            snake_key = camel_to_snake(camel_key)
            value = data.get(camel_key, data.get(snake_key))
            if value is not None:
                if isinstance(fld.type, type) and issubclass(fld.type, DynamicConversion):
                    field_dict[fld.name] = fld.type.from_dict(value)
                elif getattr(fld.type, '__origin__', None) == List and issubclass(fld.type.__args__[0], DynamicConversion):
                    field_dict[fld.name] = [fld.type.__args__[0].from_dict(item) for item in value]
                else:
                    field_dict[fld.name] = value
            elif fld.default is not fld.default_factory:
                field_dict[fld.name] = fld.default
        return cls(**field_dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        for fld in self.__dataclass_fields__.values():
            value = getattr(self, fld.name)
            if isinstance(value, DynamicConversion):
                result[snake_to_camel(fld.name)] = value.to_dict()
            elif isinstance(value, list) and value and isinstance(value[0], DynamicConversion):
                result[snake_to_camel(fld.name)] = [item.to_dict() for item in value]
            elif value is not None:
                result[snake_to_camel(fld.name)] = value
        return result


@dataclass
class SodaUser(DynamicConversion):
    user_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None


@dataclass
class SodaDatasetOwner(DynamicConversion):
    type: Optional[str] = None
    user: Optional[SodaUser] = None


@dataclass
class SodaDataConnection(DynamicConversion):
    name: Optional[str] = None
    type: Optional[str] = None
    prefix: Optional[str] = None


@dataclass
class SodaDataset(DynamicConversion):
    id: Optional[str] = None
    name: Optional[str] = None
    label: Optional[str] = None
    qualified_name: Optional[str] = None
    last_updated: Optional[str] = None
    datasource: Optional[SodaDataConnection] = None
    data_quality_status: Optional[str] = None
    health_status: Optional[int] = None
    checks: Optional[int] = None
    incidents: Optional[int] = None
    cloud_url: Optional[str] = None
    owners: List[SodaDatasetOwner] = field(default_factory=list)

    def __post_init__(self):
        if self.last_updated:
            try:
                self.last_updated = datetime.fromisoformat(self.last_updated.rstrip('Z'))
            except ValueError:
                logger.warning(f"Invalid datetime format for last_updated: {self.last_updated}")
                self.last_updated = None

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if isinstance(self.last_updated, datetime):
            result['lastUpdated'] = self.last_updated.isoformat() + 'Z'
        return result


@dataclass
class SodaApiResponse(DynamicConversion):
    content: List[SodaDataset] = field(default_factory=list)
    total_elements: Optional[int] = None
    total_pages: Optional[int] = None
    number: Optional[int] = None
    size: Optional[int] = None
    last: Optional[bool] = None
    first: Optional[bool] = None


@dataclass
class SodaCheck(DynamicConversion):
    id: Optional[str] = None
    name: Optional[str] = None
    evaluationStatus: Optional[str] = None
    lastCheckRunTime: Optional[str] = None
    definition: Optional[str] = None
    datasets: List[SodaDataset] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    owner: Optional[SodaUser] = None
    agreements: List[Any] = field(default_factory=list)
    incidents: List[Any] = field(default_factory=list)
    cloudUrl: Optional[str] = None
    lastUpdated: Optional[str] = None
    group: Dict[str, Any] = field(default_factory=dict)
    lastCheckResultValue: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.lastCheckRunTime:
            try:
                self.lastCheckRunTime = datetime.fromisoformat(self.lastCheckRunTime.rstrip('Z'))
            except ValueError:
                logger.warning(f"Invalid datetime format for lastCheckRunTime: {self.lastCheckRunTime}")
                self.lastCheckRunTime = None
        if self.lastUpdated:
            try:
                self.lastUpdated = datetime.fromisoformat(self.lastUpdated.rstrip('Z'))
            except ValueError:
                logger.warning(f"Invalid datetime format for lastUpdated: {self.lastUpdated}")
                self.lastUpdated = None

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if isinstance(self.lastCheckRunTime, datetime):
            result['lastCheckRunTime'] = self.lastCheckRunTime.isoformat() + 'Z'
        if isinstance(self.lastUpdated, datetime):
            result['lastUpdated'] = self.lastUpdated.isoformat() + 'Z'
        return result


@dataclass
class SodaCheckResultOwner(DynamicConversion):
    id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    job_title: Optional[str] = None
    phone_number: Optional[str] = None
    user_type: Optional[str] = None


@dataclass
class SodaCheckResult(DynamicConversion):
    result_id: Optional[str] = None
    scan_time: Optional[str] = None
    level: Optional[str] = None
    check_id: Optional[str] = None
    check_name: Optional[str] = None
    created_at: Optional[str] = None
    dataset_id: Optional[str] = None
    organization_id: Optional[str] = None
    metric_value: Optional[float] = None
    owner: Optional[SodaCheckResultOwner] = None
    attributes: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.scan_time:
            try:
                self.scan_time = datetime.fromisoformat(self.scan_time.rstrip('Z'))
            except ValueError:
                logger.warning(f"Invalid datetime format for scan_time: {self.scan_time}")
                self.scan_time = None
        if self.created_at:
            try:
                self.created_at = datetime.fromisoformat(self.created_at.rstrip('Z'))
            except ValueError:
                logger.warning(f"Invalid datetime format for created_at: {self.created_at}")
                self.created_at = None
        if isinstance(self.owner, dict):
            self.owner = SodaCheckResultOwner.from_dict(self.owner)

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if isinstance(self.scan_time, datetime):
            result['scanTime'] = self.scan_time.isoformat() + 'Z'
        if isinstance(self.created_at, datetime):
            result['createdAt'] = self.created_at.isoformat() + 'Z'
        return result


@dataclass
class SodaCheckResultsResponse(DynamicConversion):
    total: Optional[int] = None
    page: Optional[int] = None
    size: Optional[int] = None
    data: List[SodaCheckResult] = field(default_factory=list)

    def __post_init__(self):
        self.data = [SodaCheckResult.from_dict(item) if isinstance(item, dict) else item for item in self.data]


@dataclass
class SodaSourceReport(SourceReport):
    checks_scanned: int = 0
    checks_failed: int = 0
    datasets_scanned: int = 0

    def report_check_scanned(self) -> None:
        self.checks_scanned += 1

    def report_check_failed(self) -> None:
        self.checks_failed += 1

    def report_dataset_scanned(self) -> None:
        self.datasets_scanned += 1


class SourceMappingConfig(BaseModel):
    platform_instance: Optional[str] = Field(
        default="",
        description="Platform instance for the upstream source in DataHub.",
    )
    env: Optional[str] = Field(
        default=FabricTypeClass.PROD,
        description="Platform instance for the upstream source in DataHub.",
    )


class SodaSourceConfig(ConfigModel):

    api_key_id: Optional[str] = Field(
        default=None,
        description="Soda API Key",
    )

    api_secret_key_id: Optional[str] = Field(
        default=None,
        description="Soda API Secret Key ID",
    )

    hostname: Optional[str] = Field(
        default="cloud.soda.io",
        description="Hostname or IP Address of the Dremio server",
    )

    platform_instance: Optional[str] = Field(
        default="",
        description="Platform instance for the source.",
    )

    env: str = Field(
        default=FabricTypeClass.PROD,
        description="Environment to use in namespace when constructing URNs.",
    )

    source_mapping: Dict[str, SourceMappingConfig] = Field(
        default_factory=dict,
        description="Mapping of Soda datasource names to their corresponding platform_instance and env.",
    )

    convert_urns_to_lowercase: Optional[bool] = Field(
        default=True,
        description="Platform instance for the source.",
    )


class SodaApiClient:
    def __init__(self, api_key_id: str, api_key_secret: str, host: str):
        self.limit = 1000
        self.api_key_id = api_key_id
        self.api_key_secret = api_key_secret
        self.host = host
        self.auth = HTTPBasicAuth(
            username=self.api_key_id,
            password=self.api_key_secret,
        )
        self.headers = {
            "Content-Type": "application/json",
        }
        self.reporting_headers = {
            "Content-Type": "application/json",
            "X-API-KEY-ID": self.api_key_id,
            "X-API-KEY-SECRET": self.api_key_secret,
        }

    def _make_request(
            self,
            method: str,
            url: str,
            params: Optional[Dict[str, Any]] = None,
            json: Optional[Dict[str, Any]] = None,
    ):
        try:
            response = requests.request(
                method=method,
                url=url,
                auth=self.auth,
                headers=self.headers,
                params=params,
                json=json,
            )
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to {url}: {e}")

            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")
            raise

    def _make_reporting_request(
            self,
            method: str,
            url: str,
            json: Optional[Dict[str, Any]] = None,
    ):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.reporting_headers,
                json=json,
            )

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Error making reporting request to {url}: {e}")

            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")
            raise

    def get_datasets(self):
        datasets = []
        page = 0

        while True:
            try:
                url = f"https://{self.host}/api/v1/datasets"
                result = self._make_request(
                    method="GET",
                    url=url,
                    params={
                        "limit": self.limit,
                        "page": page
                    },
                )

                api_response = SodaApiResponse.from_dict(result)

                datasets.extend(
                    [
                        SodaDataset.from_dict(dataset)
                        if isinstance(dataset, dict)
                        else dataset
                        for dataset in api_response.content
                    ]
                )

                if api_response.last:
                    break
                page += 1

            except Exception as e:
                logger.error(f"Error fetching datasets page {page}: {e}")
                break

        return datasets

    def get_checks(self):
        checks = []
        page = 0

        while True:
            try:
                url = f"https://{self.host}/api/v1/checks"
                result = self._make_request(
                    method="GET",
                    url=url,
                    params={
                        "limit": self.limit,
                        "page": page,
                    },
                )

                api_response = SodaApiResponse.from_dict(result)

                checks.extend(
                    [
                        SodaCheck.from_dict(check)
                        if isinstance(check, dict)
                        else check
                        for check in api_response.content
                    ]
                )
                if api_response.last:
                    break
                page += 1

            except Exception as e:
                logger.error(f"Error fetching checks page {page}: {e}")
                break

        return checks

    def get_check_results(
            self,
            check_ids: List[str],
            dataset_ids: List[str],
            page: int = 1,
    ) -> SodaCheckResultsResponse:
        url = f"https://reporting.{self.host}/v1/quality/check_results"

        body = {
            "checkIds": check_ids,
            "datasetIds": dataset_ids,
            "page": page,
            "size": self.limit
        }

        try:
            result = self._make_reporting_request(
                method="POST",
                url=url,
                json=body,
            )
            return SodaCheckResultsResponse.from_dict(result)

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching check results: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")

            return SodaCheckResultsResponse(
                total=0,
                page=page,
                size=self.limit,
            )


class SodaSource(Source):
    config: SodaSourceConfig
    report: SodaSourceReport
    soda_client: SodaApiClient

    def __init__(self, config: SodaSourceConfig, ctx: PipelineContext):
        super().__init__(ctx)
        self.config = config
        self.report = SodaSourceReport()
        self.soda_client = SodaApiClient(
            api_key_id=config.api_key_id,
            api_key_secret=config.api_secret_key_id,
            host=config.hostname
        )

    @classmethod
    def create(cls, config_dict: Dict, ctx: PipelineContext) -> "SodaSource":
        config = SodaSourceConfig.parse_obj(config_dict)
        return cls(config, ctx)

    def get_platform_info(self, dataset: SodaDataset) -> Dict[str, str]:
        if dataset.datasource:
            datasource_name = dataset.datasource[
                "name"
            ] if isinstance(dataset.datasource, dict) else dataset.datasource.name
            datasource_type = dataset.datasource[
                "type"
            ] if isinstance(dataset.datasource, dict) else dataset.datasource.type

            source_info = self.config.source_mapping.get(datasource_name)
            if source_info:
                return {
                    "platform": datasource_type,
                    "platform_instance": source_info.platform_instance or None,
                    "env": source_info.env or self.config.env,
                }

        # If no mapping is present or datasource is None, use default values
        return {
            "platform": dataset.datasource['type'] if isinstance(
                dataset.datasource,
                dict,
            ) and 'type' in dataset.datasource
            else "external",
            "platform_instance": self.config.platform_instance or None,
            "env": self.config.env,
        }

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        try:
            datasets = self.soda_client.get_datasets()
            checks = self.soda_client.get_checks()

            dataset_lookup = {
                dataset.id: dataset
                for dataset in datasets
                if isinstance(dataset, SodaDataset)
            }

            for check in checks:
                self.report.report_check_scanned()

                if isinstance(check, dict):
                    check = SodaCheck.from_dict(check)

                check_datasets = [
                    SodaDataset.from_dict(dataset)
                    if isinstance(dataset, dict)
                    else dataset
                    for dataset in check.datasets
                ]

                check_results = self.soda_client.get_check_results(
                    check_ids=[check.id],
                    dataset_ids=[
                        dataset.id
                        for dataset in check_datasets
                        if dataset.id
                    ],
                )

                check_results_lookup = {
                    result.dataset_id: result
                    for result in check_results.data
                }

                for check_dataset in check_datasets:
                    if not check_dataset.id:
                        logger.warning(
                            f"Dataset in check {check.name} has no ID. Skipping.",
                       )
                        continue

                    full_dataset = dataset_lookup.get(check_dataset.id)
                    if not full_dataset:
                        logger.warning(
                            f"No matching dataset found for check: {check.name} (Dataset ID: {check_dataset.id})"
                        )
                        continue

                    self.report.report_dataset_scanned()

                    platform_info = self.get_platform_info(full_dataset)
                    dataset_urn = make_dataset_urn_with_platform_instance(
                        platform=platform_info.get("platform"),
                        name=full_dataset.qualified_name.lower()
                        if self.config.convert_urns_to_lowercase
                        else full_dataset.qualified_name,
                        platform_instance=platform_info.get("platform_instance", None),
                        env=platform_info.get("env"),
                    )

                    assertion_urn = make_assertion_urn(
                        assertion_id=f"{dataset_urn}:{check.name}",
                    )

                    yield from self._emit_assertion_wu(
                        check=check,
                        dataset_urn=dataset_urn,
                        assertion_urn=assertion_urn,
                    )

                    check_result = check_results_lookup.get(check_dataset.id)
                    if check_result:
                        yield from self._emit_assertion_result_wu(
                            result=check_result,
                            dataset_urn=dataset_urn,
                            assertion_urn=assertion_urn,
                        )

        except Exception as e:
            logger.error(f"Error fetching workunits: {e}")
            logger.exception("Detailed traceback:")

    def _emit_assertion_wu(
            self,
            check: SodaCheck,
            dataset_urn: str,
            assertion_urn: str,
    ) -> Iterable[MetadataWorkUnit]:
        try:
            assertion_info = self._create_assertion_info(
                check=check,
                dataset_urn=dataset_urn,
            )

            mcp = MetadataChangeProposalWrapper(
                entityType="assertion",
                entityUrn=assertion_urn,
                aspectName="assertionInfo",
                aspect=assertion_info,
            )

            wu = MetadataWorkUnit(
                id=f"{assertion_urn}-assertionInfo",
                mcp=mcp,
            )

            self.report.report_workunit(wu=wu)
            yield wu

        except Exception as e:
            logger.error(f"Error emitting assertion workunit: {e}")
            logger.exception("Detailed traceback:")

    def _create_assertion_info(
        self,
        check: SodaCheck,
        dataset_urn: str,
    ) -> AssertionInfoClass:
        custom_properties = {
            "soda_check_id": check.id,
            "evaluation_status": check.evaluationStatus,
            "last_check_run_time": check.lastCheckRunTime.isoformat()
                if check.lastCheckRunTime
                else "",
        }

        native_parameters = {
            "definition": check.definition or "",
        }

        return AssertionInfoClass(
            type=AssertionTypeClass.DATASET,
            datasetAssertion=DatasetAssertionInfoClass(
                dataset=dataset_urn,
                scope=DatasetAssertionScopeClass.DATASET_COLUMN
                if check.attributes.get('column')
                else DatasetAssertionScopeClass.DATASET_ROWS,
                operator="_NATIVE_",
                aggregation="_NATIVE_",
                fields=[
                    make_schema_field_urn(
                        parent_urn=dataset_urn,
                        field_path=check.attributes.get('column'),
                    )
                ] if check.attributes.get('column')
                else None,
                nativeType=check.name,
                nativeParameters=native_parameters,
                logic=check.definition,
            ),
            customProperties=custom_properties,
        )

    def _emit_assertion_result_wu(
            self,
            result: SodaCheckResult,
            dataset_urn: str,
            assertion_urn: str,
    ) -> Iterable[MetadataWorkUnit]:
        try:
            assertion_result = self._create_assertion_result(
                result=result,
                assertion_urn=assertion_urn,
                dataset_urn=dataset_urn,
            )

            mcp = MetadataChangeProposalWrapper(
                entityType="assertion",
                entityUrn=assertion_urn,
                aspectName="assertionRunEvent",
                aspect=assertion_result,
            )

            wu = MetadataWorkUnit(
                id=f"{assertion_urn}-assertionRunEvent",
                mcp=mcp,
            )

            self.report.report_workunit(wu)
            yield wu

        except Exception as e:
            logger.error(f"Error emitting assertion result workunit: {e}")
            logger.exception("Detailed traceback:")

    def _create_assertion_result(
            self,
            result: SodaCheckResult,
            assertion_urn: str,
            dataset_urn: str,
    ) -> AssertionRunEventClass:

        def flatten_and_stringify(obj: Any, parent_key: str = '') -> Dict[str, str]:
            items = {}
            if isinstance(obj, dict):
                for k, v in obj.items():
                    new_key = f"{parent_key}.{k}" if parent_key else k
                    items.update(flatten_and_stringify(v, new_key))
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    new_key = f"{parent_key}[{i}]"
                    items.update(flatten_and_stringify(v, new_key))
            else:
                items[parent_key] = str(obj) if obj is not None else ""
            return items

        native_results = flatten_and_stringify(result.to_dict())

        partition_spec = PartitionSpecClass(
            type=PartitionTypeClass.FULL_TABLE,
            partition="FULL_TABLE_SNAPSHOT"
        )

        return AssertionRunEventClass(
            timestampMillis=int(
                result.scan_time.timestamp() * 1000
            ) if result.scan_time
            else int(datetime.now().timestamp() * 1000),
            assertionUrn=assertion_urn,
            asserteeUrn=dataset_urn,
            runId=result.result_id,
            result=AssertionResultClass(
                type=AssertionResultTypeClass.SUCCESS
                if result.level == 'pass'
                else AssertionResultTypeClass.FAILURE,
                nativeResults=native_results,
            ),
            status=AssertionRunStatusClass.COMPLETE,
            partitionSpec=partition_spec,
        )

    def get_report(self):
        return self.report

    def close(self):
        pass