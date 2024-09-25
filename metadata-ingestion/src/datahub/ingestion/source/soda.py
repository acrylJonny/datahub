import logging
from typing import Iterable, List, Optional, Dict, Any, Union
from dataclasses import dataclass, field
from datetime import datetime

from soda.scan import Scan
from soda.sampler.sample_ref import SampleRef

from datahub.configuration.common import ConfigModel
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.source import Source, SourceReport
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeProposal
from datahub.metadata.schema_classes import (
    ChangeTypeClass,
    DatasetPropertiesClass,
    AssertionInfoClass,
    AssertionResultClass,
    AssertionRunEventClass,
    AssertionRunStatusClass,
    AssertionTypeClass,
    DatasetProfileClass,
    DatasetFieldProfileClass,
    DatasetAssertionInfoClass,
    AssertionResultTypeClass,
    AssertionSourceClass,
    TimeWindowSizeClass,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.utilities.urns.dataset_urn import DatasetUrn
from datahub.utilities.urns.assertion_urn import AssertionUrn

logger = logging.getLogger(__name__)


@dataclass
class SodaSourceReport(SourceReport):
    checks_scanned: int = 0
    sources_mapped: int = 0

    def report_failure(
            self,
            message: str,
            context: Optional[str] = None,
            title: Optional[str] = None,
            exc: Optional[BaseException] = None,
            log: bool = True
    ) -> None:
        super().report_failure(
            LiteralString(message),
            context,
            title,
            exc,
            log,
        )


class SodaSourceConfig(ConfigModel):
    soda_configuration_yaml_path: str
    soda_checks_yaml_path: str
    environment: str
    platform_instance: Optional[str] = None
    source_mapping: Dict[str, str] = {}
    default_platform: str = "external"


class SodaSource(Source):
    config: SodaSourceConfig
    report: SodaSourceReport
    platform_mapping: Dict[str, str]

    def __init__(self, config: SodaSourceConfig, ctx: PipelineContext):
        super().__init__(ctx)
        self.config = config
        self.report = SodaSourceReport()
        self.platform_mapping = {}

    @classmethod
    def create(cls, config_dict: Dict, ctx: PipelineContext) -> "SodaSource":
        config = SodaSourceConfig.parse_obj(config_dict)
        return cls(config, ctx)

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        scan = Scan()
        scan.add_configuration_yaml_file(self.config.soda_configuration_yaml_path)
        scan.add_sodacl_yaml_files(self.config.soda_checks_yaml_path)

        scan_result = scan.execute()
        self._build_platform_mapping(scan_result.data_source_properties)

        for check in scan_result.checks:
            source_platform = self._get_platform_from_check(check)
            dataset_name = check.table_name
            dataset_urn = DatasetUrn.create_from_ids(
                platform_id=source_platform,
                table_name=dataset_name,
                env=self.config.environment,
                platform_instance=self.config.platform_instance
            )
            assertion_urn = AssertionUrn.create_from_ids(
                dataset_urn,
                "soda",
                check.name
            )

            yield from self._emit_assertion_mcp(check, dataset_urn, assertion_urn)
            yield from self._emit_profile_mcp(check, dataset_urn)

        self.report.checks_scanned += 1

    def _build_platform_mapping(self, data_source_properties: Dict[str, Any]) -> None:
        for source_name, properties in data_source_properties.items():
            connection_type = properties.get("connection_type")
            if connection_type in self.config.source_mapping:
                self.platform_mapping[source_name] = self.config.source_mapping[connection_type]
            else:
                self.platform_mapping[source_name] = self.config.default_platform
        self.report.sources_mapped = len(self.platform_mapping)

    def _get_platform_from_check(self, check) -> str:
        source_name = check.data_source_name
        return self.platform_mapping.get(source_name, self.config.default_platform)

    def _emit_assertion_mcp(self, check, dataset_urn: DatasetUrn, assertion_urn: AssertionUrn) -> Iterable[
        MetadataWorkUnit]:
        # Emit Assertion Info
        assertion_info_mcp = MetadataChangeProposalWrapper(
            entityType="assertion",
            entityUrn=str(assertion_urn),
            changeType=ChangeTypeClass.UPSERT,
            aspectName="assertionInfo",
            aspect=self._create_assertion_info(check, dataset_urn, assertion_urn)
        )
        yield MetadataWorkUnit(id=f"{assertion_urn}-info", mcp=assertion_info_mcp)

        # Emit Assertion Result
        assertion_result_mcp = MetadataChangeProposalWrapper(
            entityType="assertion",
            entityUrn=str(assertion_urn),
            changeType=ChangeTypeClass.UPSERT,
            aspectName="assertionRunEvent",
            aspect=self._create_assertion_result(check, assertion_urn)
        )
        yield MetadataWorkUnit(id=f"{assertion_urn}-result", mcp=assertion_result_mcp)

    def _emit_profile_mcp(self, check, dataset_urn: DatasetUrn) -> Iterable[MetadataWorkUnit]:
        if check.metrics:
            profile = self._create_dataset_profile(check)
            profile_mcp = MetadataChangeProposalWrapper(
                entityType="dataset",
                entityUrn=str(dataset_urn),
                changeType=ChangeTypeClass.UPSERT,
                aspectName="datasetProfile",
                aspect=profile
            )
            yield MetadataWorkUnit(id=f"{dataset_urn}-profile", mcp=profile_mcp)

    def _create_assertion_info(self, check, dataset_urn: DatasetUrn, assertion_urn: AssertionUrn) -> AssertionInfoClass:
        dataset_assertion = DatasetAssertionInfoClass(
            dataset=str(dataset_urn),
            scope="DATASET_COLUMN" if check.column_name else "DATASET",
            fields=[
                check.column_name
            ],
        )
        return AssertionInfoClass(
            type=AssertionTypeClass.QUALITY,
            customProperties={"soda_check_type": check.check_type},
            datasetAssertion=dataset_assertion,
            source=AssertionSourceClass.EXTERNAL,
            description=check.expression
        )

    def _create_assertion_result(self, check, assertion_urn: AssertionUrn) -> AssertionRunEventClass:
        result = AssertionResultClass(
            type=AssertionResultTypeClass.SUCCESS if check.outcome == "pass" else AssertionResultTypeClass.FAILURE,
            nativeResults=self._format_check_result(check),
        )
        return AssertionRunEventClass(
            timestampMillis=int(datetime.utcnow().timestamp() * 1000),
            runId=f"soda-run-{datetime.utcnow().isoformat()}",
            asserteeUrn=str(assertion_urn),
            status=AssertionRunStatusClass.COMPLETE,
            assertionUrn=str(assertion_urn),
            result=result,
            eventGranularity=TimeWindowSizeClass.DAY
        )

    def _create_dataset_profile(self, check) -> DatasetProfileClass:
        field_profiles = []
        for metric_name, metric_value in check.metrics.items():
            if check.column_name:
                field_profile = DatasetFieldProfileClass(
                    fieldPath=check.column_name,
                    sampleValues=[str(metric_value)],
                )
                field_profiles.append(field_profile)

        return DatasetProfileClass(
            timestampMillis=int(datetime.utcnow().timestamp() * 1000),
            rowCount=check.metrics.get("row_count"),
            columnCount=len(field_profiles),
            fieldProfiles=field_profiles,
        )

    def _format_check_result(self, check) -> Dict:
        result = {
            "type": check.check_type,
            "outcome": check.outcome,
            "definition": check.definition,
            "metrics": check.metrics,
        }
        if isinstance(check.sample_ref, SampleRef):
            result["sample"] = {
                "id": check.sample_ref.sample_id,
                "table_name": check.sample_ref.table_name,
                "column_name": check.sample_ref.column_name,
            }
        return result

    def get_report(self):
        return self.report

    def close(self):
        pass