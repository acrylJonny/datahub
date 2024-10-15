import yaml
import logging
from typing import Iterable, Dict, Any, Optional, List, Union
from dataclasses import field, dataclass
from datetime import datetime
import requests
from requests.auth import HTTPBasicAuth

from datahub.configuration.common import ConfigModel
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.source import Source, SourceReport
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.emitter.mce_builder import make_dataset_urn_with_platform_instance, make_assertion_urn, \
    make_schema_field_urn
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
        for field in cls.__dataclass_fields__.values():
            camel_key = snake_to_camel(field.name)
            snake_key = camel_to_snake(camel_key)
            value = data.get(camel_key, data.get(snake_key))
            if value is not None:
                if isinstance(field.type, type) and issubclass(field.type, DynamicConversion):
                    field_dict[field.name] = field.type.from_dict(value)
                elif getattr(field.type, '__origin__', None) == List and issubclass(field.type.__args__[0], DynamicConversion):
                    field_dict[field.name] = [field.type.__args__[0].from_dict(item) for item in value]
                else:
                    field_dict[field.name] = value
            elif field.default is not field.default_factory:
                field_dict[field.name] = field.default
        return cls(**field_dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        for field in self.__dataclass_fields__.values():
            value = getattr(self, field.name)
            if isinstance(value, DynamicConversion):
                result[snake_to_camel(field.name)] = value.to_dict()
            elif isinstance(value, list) and value and isinstance(value[0], DynamicConversion):
                result[snake_to_camel(field.name)] = [item.to_dict() for item in value]
            elif value is not None:
                result[snake_to_camel(field.name)] = value
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


class SodaSourceConfig(ConfigModel):
    api_key: str
    api_secret: str
    host: str = "cloud.soda.io"
    platform: str
    platform_instance: Optional[str] = None
    convert_column_urns_to_lowercase: bool = False
    environment: str = "PROD"


import base64

import requests
from requests.auth import HTTPBasicAuth
import logging

logger = logging.getLogger(__name__)


class SodaApiClient:
    def __init__(self, api_key: str, api_secret: str, host: str):
        self.limit = 1000
        self.api_key = api_key
        self.api_secret = api_secret
        self.host = host
        self.auth = HTTPBasicAuth(self.api_key, self.api_secret)
        self.headers = {
            "Content-Type": "application/json"
        }
        self.token = None

    def _make_request(self, method: str, url: str, params: dict = None, json: dict = None):
        try:
            response = requests.request(
                method,
                url,
                auth=self.auth,
                headers=self.headers,
                params=params,
                json=json
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to {url}: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")
            raise

    def _get_token(self):
        if self.token is None:
            url = f"https://reporting.{self.host}/v1/get_token"
            try:
                response = requests.post(url, auth=self.auth)
                response.raise_for_status()
                self.token = response.json().get('token')
            except requests.exceptions.RequestException as e:
                logger.error(f"Error getting token: {e}")
                if hasattr(e.response, 'text'):
                    logger.error(f"Response content: {e.response.text}")
                raise
        return self.token

    def _make_reporting_request(self, method: str, url: str, json: dict = None):
        token = self._get_token()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to {url}: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")
            raise

    def get_datasets(self):
        datasets = []
        page = 0
        while True:
            try:
                url = f"https://{self.host}/api/v1/datasets"
                result = self._make_request(method="GET", url=url, params={"limit": self.limit, "page": page})
                api_response = SodaApiResponse.from_dict(result)
                datasets.extend([SodaDataset.from_dict(dataset) if isinstance(dataset, dict) else dataset for dataset in
                                 api_response.content])
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
                result = self._make_request(method="GET", url=url, params={"limit": self.limit, "page": page})
                api_response = SodaApiResponse.from_dict(result)
                checks.extend([SodaCheck.from_dict(check) if isinstance(check, dict) else check for check in
                               api_response.content])
                if api_response.last:
                    break
                page += 1
            except Exception as e:
                logger.error(f"Error fetching checks page {page}: {e}")
                break
        return checks

    def get_check_results(self, check_ids: List[str], dataset_ids: List[str], page: int = 1, size: int = 100):
        url = f"https://reporting.{self.host}/v1/quality/check_results"

        body = {
            "checkIds": check_ids,
            "datasetIds": dataset_ids,
            "page": page,
            "size": size
        }

        try:
            result = self._make_reporting_request(
                method="POST",
                url=url,
                json=body
            )
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching check results: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")
            return None


class SodaSource(Source):
    config: SodaSourceConfig
    report: SodaSourceReport
    soda_client: SodaApiClient

    def __init__(self, config: SodaSourceConfig, ctx: PipelineContext):
        super().__init__(ctx)
        self.config = config
        self.report = SodaSourceReport()
        self.soda_client = SodaApiClient(
            api_key=config.api_key,
            api_secret=config.api_secret,
            host=config.host
        )

    @classmethod
    def create(cls, config_dict: Dict, ctx: PipelineContext) -> "SodaSource":
        config = SodaSourceConfig.parse_obj(config_dict)
        return cls(config, ctx)

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        try:
            datasets = self.soda_client.get_datasets()
            checks = self.soda_client.get_checks()

            dataset_lookup = {dataset.id: dataset for dataset in datasets if isinstance(dataset, SodaDataset)}

            for check in checks:
                self.report.report_check_scanned()

                if isinstance(check, dict):
                    check = SodaCheck.from_dict(check)

                check_results = self.soda_client.get_check_results(
                    check_ids=[check.id],
                    dataset_ids=[ds.get("id") for ds in check.datasets]
                )

                for check_dataset in check.datasets:
                    if isinstance(check_dataset, dict):
                        check_dataset = SodaDataset.from_dict(check_dataset)

                    full_dataset = dataset_lookup.get(check_dataset.id)
                    if not full_dataset:
                        logger.warning(
                            f"No matching dataset found for check: {check.name} (Dataset ID: {check_dataset.id})")
                        continue

                    self.report.report_dataset_scanned()

                    dataset_urn = make_dataset_urn_with_platform_instance(
                        platform=self.config.platform,
                        name=full_dataset.qualified_name.lower() if self.config.convert_column_urns_to_lowercase else full_dataset.qualified_name,
                        platform_instance=self.config.platform_instance,
                        env=self.config.environment,
                    )

                    assertion_urn = make_assertion_urn(assertion_id=f"{dataset_urn}:{check.name}")

                    yield from self._emit_assertion_wu(check=check, dataset_urn=dataset_urn,
                                                       assertion_urn=assertion_urn, dataset=full_dataset)

                    if check_results and check_results.get('content'):
                        yield from self._emit_assertion_result_wu(result=check_results['content'][0],
                                                                  dataset_urn=dataset_urn, assertion_urn=assertion_urn)

        except Exception as e:
            logger.error(f"Error fetching workunits: {e}")
            logger.exception("Detailed traceback:")

    def _emit_assertion_wu(
            self,
            check: SodaCheck,
            dataset_urn: str,
            assertion_urn: str,
            dataset: SodaDataset,
    ) -> Iterable[MetadataWorkUnit]:
        try:
            assertion_info = self._create_assertion_info(
                check=check,
                dataset_urn=dataset_urn,
                dataset=dataset,
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
        dataset: SodaDataset,
    ) -> AssertionInfoClass:
        custom_properties = {
            "soda_check_id": check.id,
            "evaluation_status": check.evaluationStatus,
            "last_check_run_time": check.lastCheckRunTime.isoformat()
                if check.lastCheckRunTime
                else "",
        }

        native_parameters = {
            "definition": ""
                #stringify_value(check.definition),
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
            result: Dict,
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

    def _create_assertion_result(self, result: Dict, assertion_urn: str, dataset_urn: str) -> AssertionRunEventClass:
        def flatten_dict(obj: Any, prefix: str = '') -> Dict[str, str]:
            flattened = {}
            if isinstance(obj, dict):
                for k, v in obj.items():
                    new_key = f"{prefix}.{k}" if prefix else k
                    flattened.update(flatten_dict(v, new_key))
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    new_key = f"{prefix}[{i}]"
                    flattened.update(flatten_dict(item, new_key))
            else:
                flattened[prefix] = str(obj)
            return flattened

        flattened_result = flatten_dict(result)

        outcome = result.get('outcome', 'unknown')

        return AssertionRunEventClass(
            timestampMillis=int(datetime.fromisoformat(result.get('timeWindowStart', datetime.now().isoformat())).timestamp() * 1000),
            assertionUrn=assertion_urn,
            asserteeUrn=dataset_urn,
            runId=result.get('id', 'latest'),
            result=AssertionResultClass(
                type=AssertionResultTypeClass.SUCCESS if outcome == 'pass' else AssertionResultTypeClass.FAILURE,
                nativeResults=flattened_result,
            ),
            status=AssertionRunStatusClass.COMPLETE,
        )

    def get_report(self):
        return self.report

    def close(self):
        pass