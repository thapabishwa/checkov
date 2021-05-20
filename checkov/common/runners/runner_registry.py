import json
import logging
import os
from abc import abstractmethod
from collections import defaultdict

from checkov.common.bridgecrew.integration_features.integration_feature_registry import integration_feature_registry
from checkov.common.models.enums import CheckResult
from checkov.common.output.report import Report
from checkov.common.util import dict_utils
from checkov.terraform.context_parsers.registry import parser_registry
from checkov.terraform.runner import Runner as tf_runner
from checkov.terraform.parser import Parser


CHECK_BLOCK_TYPES = frozenset(["resource", "data", "provider", "module"])
OUTPUT_CHOICES = ['cli', 'json', 'junitxml', 'github_failed_only']

from checkov.common.bridgecrew.platform_integration import BcPlatformIntegration


class RunnerRegistry(object):
    runners = []
    scan_reports = []
    banner = ""

    def __init__(self, banner, runner_filter, *runners):
        self.logger = logging.getLogger(__name__)
        self.runner_filter = runner_filter
        self.runners = runners
        self.banner = banner
        self.scan_reports = []
        self.filter_runner_framework()
        self.bc_platform = BcPlatformIntegration()

    @abstractmethod
    def extract_entity_details(self, entity):
        raise NotImplementedError()

    def run(self, root_folder=None, external_checks_dir=None, files=None, guidelines=None, collect_skip_comments=True, bc_integration=None, repo_root_for_plan_enrichment=None):
        for runner in self.runners:
            integration_feature_registry.run_pre_scan()
            scan_report = runner.run(root_folder, external_checks_dir=external_checks_dir, files=files,
                                     runner_filter=self.runner_filter, collect_skip_comments=collect_skip_comments)
            integration_feature_registry.run_post_scan(scan_report)
            if guidelines:
                RunnerRegistry.enrich_report_with_guidelines(scan_report, guidelines)
            if repo_root_for_plan_enrichment:
                enriched_resources = RunnerRegistry.get_enriched_resources(scan_report, repo_root_for_plan_enrichment)
                enriched_report = RunnerRegistry.enrich_plan_report(scan_report, enriched_resources)
                enriched_report_with_skipped = RunnerRegistry.handle_skipped_checks(enriched_report, enriched_resources)
                self.scan_reports.append(enriched_report_with_skipped)
            else:
                self.scan_reports.append(scan_report)
        return self.scan_reports

    def print_reports(self, scan_reports, args, url=None):
        if args.output == 'cli':
            print(f"{self.banner}\n")
        exit_codes = []
        report_jsons = []
        junit_reports = []
        for report in scan_reports:
            if not report.is_empty():
                if args.output == "json":
                    report_jsons.append(report.get_dict(is_quiet=args.quiet))
                elif args.output == "junitxml":
                    junit_reports.append(report)
                    # report.print_junit_xml()
                elif args.output == 'github_failed_only':
                    report.print_failed_github_md()
                else:
                    report.print_console(is_quiet=args.quiet, is_compact=args.compact)
                    if url:
                        print("More details: {}".format(url))
            exit_codes.append(report.get_exit_code(args.soft_fail))
        if args.output == "junitxml":
            if len(junit_reports) == 1:
                junit_reports[0].print_junit_xml()
            else:
                master_report = Report(None)
                for report in junit_reports:
                    master_report.skipped_checks += report.skipped_checks
                    master_report.passed_checks += report.passed_checks
                    master_report.failed_checks += report.failed_checks
                master_report.print_junit_xml()
        if args.output == "json":
            if len(report_jsons) == 1:
                print(json.dumps(report_jsons[0], indent=4))
            else:
                print(json.dumps(report_jsons, indent=4))
        if args.output == "cli":
            self.bc_platform.get_report_to_platform(args,scan_reports)

        exit_code = 1 if 1 in exit_codes else 0
        return exit_code

    def filter_runner_framework(self):
        if not self.runner_filter:
            return
        if self.runner_filter.framework is None:
            return
        if self.runner_filter.framework == 'all':
            return
        filtered_runners = []
        for runner in self.runners:
            if runner.check_type in self.runner_filter.framework:
                filtered_runners.append(runner)
        self.runners = filtered_runners
        return

    @staticmethod
    def enrich_report_with_guidelines(scan_report, guidelines):
        for record in scan_report.failed_checks + scan_report.passed_checks + scan_report.skipped_checks:
            if record.check_id in guidelines:
                record.set_guideline(guidelines[record.check_id])

    @staticmethod
    def get_enriched_resources(report, repo_root):
        tf_definitions = {}
        parsing_errors = {}
        Parser().parse_directory(
            directory=repo_root,  # assume plan file is in the repo-root
            out_definitions=tf_definitions,
            out_parsing_errors=parsing_errors,
        )

        enriched_resources = defaultdict(dict)
        for full_file_path, definition in tf_definitions.items():
            definitions_context = parser_registry.enrich_definitions_context((full_file_path, definition))
            abs_scanned_file, _ = tf_runner._strip_module_referrer(full_file_path)
            scanned_file = os.path.relpath(abs_scanned_file, repo_root)
            for block_type, block_value in definition.items():
                if block_type in CHECK_BLOCK_TYPES:
                    for entity in block_value:
                        context_parser = parser_registry.context_parsers[block_type]
                        definition_path = context_parser.get_entity_context_path(entity)
                        entity_id = ".".join(definition_path)
                        entity_context_path = [block_type] + definition_path
                        entity_context = dict_utils.getInnerDict(
                            definitions_context[full_file_path], entity_context_path
                        )
                        entity_lines_range = [
                            entity_context.get("start_line"),
                            entity_context.get("end_line"),
                        ]
                        entity_code_lines = entity_context.get("code_lines")
                        skipped_checks = entity_context.get("skipped_checks")
                        enriched_resources[entity_id] = {
                            "entity_code_lines": entity_code_lines,
                            "entity_lines_range": entity_lines_range,
                            "scanned_file": scanned_file,
                            "skipped_checks": skipped_checks
                        }
        return enriched_resources

    @staticmethod
    def enrich_plan_report(report, enriched_resources):
        # This enriches reports with the appropriate filepath, line numbers, and codeblock
        for record in report.failed_checks:
            enriched_resource = enriched_resources.get(record.resource)
            if enriched_resource:
                record.file_path = enriched_resource["scanned_file"]
                record.file_line_range = enriched_resource["entity_lines_range"]
                record.code_block = enriched_resource["entity_code_lines"]
        return report

    @staticmethod
    def handle_skipped_checks(report, enriched_resources):
        records_to_skip = []
        for record in report.failed_checks:
            resource_skips = enriched_resources[record.resource]["skipped_checks"]
            for skip in resource_skips:
                if record.check_id in skip["id"]: 
                    # Remove and re-add the record to make Checkov mark it as skipped
                    report.failed_checks.remove(record)
                    record.check_result["result"] = CheckResult.SKIPPED
                    record.check_result["suppress_comment"] = skip["suppress_comment"]
                    report.add_record(record)
        return report
