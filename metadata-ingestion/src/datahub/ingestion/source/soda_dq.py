import logging
from typing import Dict, Iterable, Optional, List
from dataclasses import dataclass
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


class SodaApiClient:
    def __init__(self, api_key: str, api_secret: str, host: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.host = host
        self.auth = HTTPBasicAuth(self.api_key, self.api_secret)
        self.headers = {
            "Content-Type": "application/json"
        }

    def _make_request(self, method: str, endpoint: str, params: dict = None):
        url = f"https://{self.host}/api/v1/{endpoint}"
        try:
            response = requests.request(
                method,
                url,
                auth=self.auth,
                headers=self.headers,
                params=params
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to {url}: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response content: {e.response.text}")
            raise

    def get_checks(self):
        return self._make_request("GET", "checks")

    def get_dataset_info(self, dataset_id: str):
        return self._make_request("GET", f"datasets/{dataset_id}")

    def get_check_results(self, check_id: str, limit: int = 1):
        return self._make_request("GET", f"checks/{check_id}/results", params={"limit": limit})


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
            checks_response = self.soda_client.get_checks()
            checks = checks_response.get('content', [])

            for check in checks:
                self.report.report_check_scanned()
                datasets = check.get('datasets', [])
                logger.error(datasets)

                if not datasets:
                    logger.warning(f"No datasets found for check: {check.get('name', 'unknown')}")
                    continue

                for dataset in datasets:
                    dataset_id = dataset.get('id')
                    if not dataset_id:
                        logger.warning(f"No dataset ID found for check: {check.get('name', 'unknown')}")
                        continue

                    dataset_info = self.soda_client.get_dataset_info(dataset_id)
                    logger.error(dataset_info)
                    dataset_name = dataset_info.get('name', 'unknown')
                    database_name = dataset_info.get('databaseName', 'unknown')
                    source_type = dataset_info.get('dataSourceType', 'unknown')

                    self.report.report_dataset_scanned()

                    dataset_urn = make_dataset_urn_with_platform_instance(
                        self.config.platform,
                        f"{database_name}.{dataset_name}".lower() if self.config.convert_column_urns_to_lowercase else f"{database_name}.{dataset_name}",
                        self.config.platform_instance,
                        self.config.environment
                    )

                    assertion_urn = make_assertion_urn(f"{dataset_urn}:{check.get('name', 'unknown')}")
                    yield from self._emit_assertion_wu(check, dataset_urn, assertion_urn, dataset_info)

                    # Use the last check result value if available
                    last_check_result = check.get('lastCheckResultValue', {})
                    if last_check_result:
                        yield from self._emit_assertion_result_wu(check, last_check_result, dataset_urn, assertion_urn)

        except Exception as e:
            logger.error(f"Error fetching workunits: {e}")

    def _emit_assertion_wu(self, check: Dict, dataset_urn: str, assertion_urn: str, dataset_info: Dict) -> Iterable[
        MetadataWorkUnit]:
        try:
            assertion_info = self._create_assertion_info(check, dataset_urn, dataset_info)
            mcp = MetadataChangeProposalWrapper(
                entityType="assertion",
                entityUrn=assertion_urn,
                aspectName="assertionInfo",
                aspect=assertion_info,
            )
            wu = MetadataWorkUnit(id=f"{assertion_urn}-assertionInfo", mcp=mcp)
            self.report.report_workunit(wu)
            yield wu
        except Exception as e:
            logger.error(f"Error emitting assertion workunit: {e}")

    def _emit_assertion_result_wu(self, check: Dict, result: Dict, dataset_urn: str, assertion_urn: str) -> Iterable[
        MetadataWorkUnit]:
        try:
            assertion_result = self._create_assertion_result(result, assertion_urn, dataset_urn)
            mcp = MetadataChangeProposalWrapper(
                entityType="assertion",
                entityUrn=assertion_urn,
                aspectName="assertionRunEvent",
                aspect=assertion_result,
            )
            wu = MetadataWorkUnit(id=f"{assertion_urn}-assertionRunEvent", mcp=mcp)
            self.report.report_workunit(wu)
            yield wu
        except Exception as e:
            logger.error(f"Error emitting assertion result workunit: {e}")

    def _create_assertion_info(self, check: Dict, dataset_urn: str, dataset_info: Dict) -> AssertionInfoClass:
        return AssertionInfoClass(
            type=AssertionTypeClass.DATASET,
            datasetAssertion=DatasetAssertionInfoClass(
                dataset=dataset_urn,
                scope=DatasetAssertionScopeClass.DATASET_COLUMN if check.get(
                    'column') else DatasetAssertionScopeClass.DATASET,
                operator=AssertionTypeClass.CUSTOM,
                aggregation=None,
                fields=[make_schema_field_urn(dataset_urn, check['column'])] if check.get('column') else [],
                nativeType=check.get('name', 'unknown'),
                nativeParameters={"definition": check.get('definition', '')},
            ),
            customProperties={
                "soda_check_id": check['id'],
                "evaluation_status": check.get('evaluationStatus', 'unknown'),
                "last_check_run_time": check.get('lastCheckRunTime', ''),
                "database_name": dataset_info.get('databaseName', 'unknown'),
                "dataset_name": dataset_info.get('name', 'unknown'),
                "source_type": dataset_info.get('dataSourceType', 'unknown'),
            },
        )

    def _create_assertion_result(self, result: Dict, assertion_urn: str, dataset_urn: str) -> AssertionRunEventClass:
        if 'valueSeries' in result:
            # For schema checks
            outcome = next((item['label'] for item in result['valueSeries']['values'] if item['value'] > 0), 'unknown')
            native_results = result['valueSeries']
        else:
            # For other checks
            outcome = 'fail' if result.get('value', 0) > 0 else 'pass'
            native_results = result

        return AssertionRunEventClass(
            timestampMillis=int(datetime.now().timestamp() * 1000),
            assertionUrn=assertion_urn,
            asserteeUrn=dataset_urn,
            runId='latest',
            result=AssertionResultClass(
                type=AssertionResultTypeClass.SUCCESS if outcome == 'pass' else AssertionResultTypeClass.FAILURE,
                nativeResults=native_results,
            ),
            status=AssertionRunStatusClass.COMPLETE,
        )

    def get_report(self):
        return self.report

    def close(self):
        pass