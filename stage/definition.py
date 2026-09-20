#!/usr/bin/env python3

"""
Built-in stage definitions for the pipeline system.
Registers all standard pipeline stages with their dependencies.
"""

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from constant.search import (
    API_LIMIT,
    API_MAX_PAGES,
    API_RESULTS_PER_PAGE,
    WEB_LIMIT,
    WEB_MAX_PAGES,
    WEB_RESULTS_PER_PAGE,
)
from constant.system import SERVICE_TYPE_GITHUB_API, SERVICE_TYPE_GITHUB_WEB
from core.enums import ErrorReason, PipelineStage, ResultType
from core.exceptions import TransientFetchError
from core.models import (
    AcquisitionTask,
    CheckTask,
    InspectTask,
    LinkMetadata,
    Patterns,
    ProviderTask,
    SearchTask,
    Service,
)
from core.types import IProvider
from search import client
from search.github.refine.engine import RefineEngine
from storage.key_ledger import KeyCheckRequest, key_hash, mask_key
from storage.registry import patterns_hash, url_hash
from tools.logger import get_logger
from tools.state import GithubCredentialLimited
from tools.utils import get_service_name, handle_exceptions

from .base import BasePipelineStage, OutputHandler, StageOutput, StageResources
from .factory import TaskFactory
from .registry import register_stage

logger = get_logger("stage")


def _refresh_repo_meta(resources: StageResources, urls: List[str]) -> None:
    """Lazily refresh stale/missing repository metadata (fail-open).

    Consulted before a stage acts on repository-level evidence so the skip
    evaluator and gather hook always see the freshest cached values.
    """
    enricher = getattr(resources, "enrichment", None)
    if enricher is None or not getattr(enricher, "enabled", False):
        return
    try:
        enricher.enrich_urls(urls)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[enrichment] repository metadata refresh failed (fail-open): {e}")


@register_stage(
    name=PipelineStage.SEARCH.value,
    depends_on=[],
    produces_for=[PipelineStage.GATHER.value, PipelineStage.CHECK.value],
    description="Search GitHub for potential API keys",
)
class SearchStage(BasePipelineStage):
    """Pipeline stage for searching GitHub with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.SEARCH.value, resources, handler, **kwargs)

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        search_task = task if isinstance(task, SearchTask) else SearchTask()
        return (
            f"{PipelineStage.SEARCH.value}:{task.provider}:{search_task.query}:{search_task.page}:{search_task.regex}"
        )

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is a SearchTask."""
        return isinstance(task, SearchTask)

    def _pre_process(self, task: ProviderTask) -> bool:
        """Pre-process search task - validate query and provider."""

        # Check if provider is enabled
        if not self.resources.is_enabled(task.provider, "search"):
            logger.debug(f"[{self.name}] search disabled for provider: {task.provider}")
            return False

        # Validate query
        search_task = task if isinstance(task, SearchTask) else None
        if not search_task or not search_task.query:
            logger.warning(f"[{self.name}] empty query for provider: {task.provider}")
            return False

        return True

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute search task processing."""
        return self._search_worker(task)

    def _search_worker(self, task: SearchTask) -> Optional[StageOutput]:
        """Pure functional search worker"""
        try:
            # API transport enriches this mapping in place; web leaves it empty.
            link_metadata: Dict[str, LinkMetadata] = {}

            # Execute search based on page number
            if task.page == 1:
                results, content, total = self._execute_first_page_search(task, link_metadata)
            else:
                results, content = self._execute_page_search(task, link_metadata)
                total = 0

            # Create output object
            output = StageOutput(task=task)

            # Extract keys directly from search content
            keys = []
            if content and task.regex:
                keys = self._extract_keys_from_content(content, task)
                for key_service in keys:
                    check_task = TaskFactory.create_check_task(task.provider, key_service)
                    output.add_task(check_task, PipelineStage.CHECK.value)

                if keys:
                    logger.info(
                        f"[{self.name}] extracted {len(keys)} keys from search content, provider: {task.provider}"
                    )

            # Create acquisition tasks for links
            if results:
                patterns = Patterns(
                    key_pattern=task.regex,
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                )
                skip_map = self._gather_skip_map(task, results, patterns)
                enforce = bool(getattr(getattr(self.resources, "gather_skip", None), "enforce", False))
                suppressed = 0
                for link in results:
                    decision = skip_map.get(link)
                    if enforce and decision is not None and decision.skip:
                        suppressed += 1
                        continue
                    acquisition_task = TaskFactory.create_acquisition_task(task.provider, link, patterns)
                    output.add_task(acquisition_task, PipelineStage.GATHER.value)

                if suppressed:
                    logger.info(
                        f"[{self.name}] suppressed {suppressed} already-known links, provider: {task.provider}"
                    )

                # Add links to be saved (audit log stays complete regardless of skips)
                output.add_links(task.provider, results)

                # Carry API-extracted freshness metadata to the registry writer.
                if link_metadata:
                    output.add_link_metadata(task.provider, link_metadata)

                # Fill-rate observability: only the API transport yields dates
                # at search stage (web dates arrive at gather stage).
                date_metrics = getattr(self.resources, "date_metrics", None)
                if date_metrics is not None and task.use_api:
                    for link in results:
                        item = link_metadata.get(link)
                        date_metrics.record_api(bool(item and item.has_repo_date))

                # Write-only hook: record discovered links
                self._record_discovered(task, results)

            # Handle pagination/refinement.  API transport may chain pages
            # through the early-stop detector; web keeps the pre-change bulk
            # generation (the detector is inert for web at code level).
            if task.page == 1 and total > 0:
                self._handle_first_page_results(task, results or [], total, output)
            elif task.page > 1:
                self._handle_page_results(task, results or [], output)

            logger.info(
                f"[{self.name}] search completed for {task.provider}: {len(results) if results else 0} links, {len(keys)} keys"
            )

            return output

        except TransientFetchError as e:
            # Typed transient failure: keep the pre-change log, then let
            # ``process_task`` apply the failure_handling mode policy.
            logger.error(f"[{self.name}] error, provider: {task.provider}, task: {task}, message: {e}")
            raise

        except Exception as e:
            logger.error(f"[{self.name}] error, provider: {task.provider}, task: {task}, message: {e}")
            return None

    def _record_discovered(self, task: SearchTask, links: List[str]) -> None:
        """Write-only hook: record discovered links in the registry."""
        registry = getattr(self.resources, "registry", None)
        if registry is None:
            return
        try:
            transport = "api" if getattr(task, "use_api", False) else "web"
            query_origin = getattr(task, "query", "") or ""
            for link in links:
                registry.record_link(
                    link,
                    transport=transport,
                    query_origin=query_origin,
                    provider=task.provider,
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[{self.name}] registry discovery hook failed: {e}")

    def _gather_skip_map(self, task: SearchTask, links: List[str], patterns: Patterns) -> Dict[str, Any]:
        """Compute the gather-skip decision map for one result page (fail-open).

        Returns ``{}`` when the engine is absent/off or the lookup errors, so
        every link produces a task in the safe direction.
        """
        engine = getattr(self.resources, "gather_skip", None)
        if engine is None or not engine.enabled:
            return {}
        # Refresh stale repository metadata before evaluating push evidence.
        _refresh_repo_meta(self.resources, links)
        try:
            digest = patterns_hash(
                key_pattern=patterns.key_pattern,
                address_pattern=patterns.address_pattern,
                endpoint_pattern=patterns.endpoint_pattern,
                model_pattern=patterns.model_pattern,
            )
            decisions = engine.decide(links, provider=task.provider, patterns_hash=digest)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[{self.name}] gather-skip decision failed: {e}")
            return {}
        return {decision.url: decision for decision in decisions}

    def _execute_first_page_search(
        self, task: SearchTask, metadata: Optional[Dict[str, LinkMetadata]] = None
    ) -> Tuple[List[str], str, int]:
        """Execute first page search and get total count in single request"""
        while True:
            # Get auth via injected provider
            if task.use_api:
                auth_token = self.resources.auth.get_token()
            else:
                auth_token = self.resources.auth.get_session()

            if not auth_token:
                return [], "", 0

            try:
                # Execute search with count - now returns content as well
                results, total, content = client.search_with_count(
                    query=self._preprocess_query(task.query, task.use_api),
                    session=auth_token,
                    page=task.page,
                    with_api=task.use_api,
                    peer_page=API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE,
                    metadata=metadata,
                )
                return results, content, total
            except GithubCredentialLimited as e:
                logger.warning(
                    f"[{self.name}] GitHub credential cooling during first-page search, "
                    f"retry with another credential, wait: {e.wait:.1f}s"
                )

    def _preprocess_query(self, query: str, use_api: bool) -> str:
        """Github Rest API search syntax don't support regex, so we need remove it if exists"""
        if use_api:
            keyword = RefineEngine.get_instance().clean_regex(query=query)
            if keyword:
                query = keyword

        return query

    def _execute_page_search(
        self, task: SearchTask, metadata: Optional[Dict[str, LinkMetadata]] = None
    ) -> Tuple[List[str], str]:
        """Execute subsequent page search in single request"""
        while True:
            # Get auth via injected provider
            if task.use_api:
                auth_token = self.resources.auth.get_token()
            else:
                auth_token = self.resources.auth.get_session()

            if not auth_token:
                return [], ""

            try:
                # Execute search - now returns content as well
                results, content = client.search_code(
                    query=self._preprocess_query(task.query, task.use_api),
                    session=auth_token,
                    page=task.page,
                    with_api=task.use_api,
                    peer_page=API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE,
                    metadata=metadata,
                )
                return results, content
            except GithubCredentialLimited as e:
                logger.warning(
                    f"[{self.name}] GitHub credential cooling during page search, "
                    f"retry with another credential, wait: {e.wait:.1f}s"
                )

    def _apply_rate_limit(self, use_api: bool) -> bool:
        """Apply rate limiting for GitHub requests"""
        service_type = SERVICE_TYPE_GITHUB_API if use_api else SERVICE_TYPE_GITHUB_WEB
        if not self.resources.limiter.acquire(service_type):
            wait_time = self.resources.limiter.wait_time(service_type)
            if wait_time > 0:
                time.sleep(wait_time)
                if not self.resources.limiter.acquire(service_type):
                    bucket = self.resources.limiter._get_bucket(service_type)
                    max_value = bucket.burst if bucket else "unknown"
                    logger.info(
                        f'[{self.name}] rate limit exceeded for Github {"Rest API" if use_api else "Web"}, max: {max_value}'
                    )
                    return False
        return True

    def _handle_first_page_results(
        self, task: SearchTask, results: List[str], total: int, output: StageOutput
    ) -> None:
        """Handle first page results - decide refinement or pagination"""
        limit = API_LIMIT if task.use_api else WEB_LIMIT
        per_page = API_RESULTS_PER_PAGE if task.use_api else WEB_RESULTS_PER_PAGE

        # If needs refine query
        if total > limit:
            # Regenerate the query with less data
            partitions = int(math.ceil(total / limit))
            queries = RefineEngine.get_instance().generate_queries(query=task.query, partitions=partitions)

            # Add new query tasks to output
            for query in queries:
                if not query:
                    logger.warning(
                        f"[{self.name}] skip refined query due to empty for query: {task.query}, provider: {task.provider}"
                    )
                    continue
                elif query == task.query:
                    logger.warning(
                        f"[{self.name}] discard refined query same as original: {query}, provider: {task.provider}"
                    )
                    continue

                refined_task = SearchTask(
                    provider=task.provider,
                    query=query,
                    regex=task.regex,
                    page=1,
                    use_api=task.use_api,
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                )

                output.add_task(refined_task, PipelineStage.SEARCH.value)

            logger.info(
                f"[{self.name}] generated {len(queries)} refined tasks for provider: {task.provider}, query: {task.query}"
            )

        # If needs pagination and not refining
        elif total > per_page:
            max_pages = min(
                math.ceil(total / per_page),
                API_MAX_PAGES if task.use_api else WEB_MAX_PAGES,
            )

            engine = self._early_stop_engine(task)
            if engine is not None:
                # API-only chained pagination: record page 1, then emit only the
                # next page so every later boundary can be evaluated.
                stopped = self._observe_page(task, results, page=1, max_pages=max_pages, engine=engine)
                if not stopped and max_pages >= 2:
                    output.add_task(self._generate_page_task(task, 2), PipelineStage.SEARCH.value)
                    logger.info(
                        f"[{self.name}] early-stop chaining page tasks for provider: {task.provider}, "
                        f"query: {task.query}, max_pages: {max_pages}"
                    )
                return

            page_tasks = self._generate_page_tasks(task, total, per_page)
            for page_task in page_tasks:
                output.add_task(page_task, PipelineStage.SEARCH.value)
            logger.info(
                f"[{self.name}] generated {len(page_tasks)} page tasks for provider: {task.provider}, query: {task.query}"
            )

    def _handle_page_results(self, task: SearchTask, results: List[str], output: StageOutput) -> None:
        """Handle a subsequent page when API early-stop chaining is active."""
        engine = self._early_stop_engine(task)
        if engine is None:
            return

        stopped = self._observe_page(task, results, page=task.page, engine=engine)
        if stopped:
            logger.info(
                f"[{self.name}] early stop suppressed page {task.page + 1} for provider: {task.provider}, "
                f"query: {task.query}"
            )
            return

        max_pages = engine.max_pages(task.provider, task.query)
        if task.page + 1 <= max_pages:
            output.add_task(self._generate_page_task(task, task.page + 1), PipelineStage.SEARCH.value)

    def _early_stop_engine(self, task: SearchTask):
        """Return the early-stop engine for API tasks, else None (web inert)."""
        if not getattr(task, "use_api", False):
            return None
        engine = getattr(self.resources, "early_stop", None)
        if engine is None or not getattr(engine, "enabled", False):
            return None
        return engine

    def _observe_page(
        self,
        task: SearchTask,
        results: List[str],
        page: int,
        max_pages: Optional[int] = None,
        engine=None,
    ) -> bool:
        """Record one page in the frontier tracker; True when it stopped."""
        try:
            digest = patterns_hash(
                key_pattern=task.regex,
                address_pattern=task.address_pattern,
                endpoint_pattern=task.endpoint_pattern,
                model_pattern=task.model_pattern,
            )
            result = engine.observe(
                provider=task.provider,
                query=task.query,
                page=page,
                links=results,
                patterns_hash=digest,
                max_pages=max_pages,
            )
        except Exception as e:  # pragma: no cover - defensive (fail open)
            logger.warning(f"[{self.name}] early-stop evaluation failed (fail-open): {e}")
            return False
        return bool(result.stopped)

    def _generate_page_task(self, task: SearchTask, page: int) -> SearchTask:
        """Build a single pagination task for ``page`` of the same partition."""
        return SearchTask(
            provider=task.provider,
            query=task.query,
            regex=task.regex,
            page=page,
            use_api=task.use_api,
            address_pattern=task.address_pattern,
            endpoint_pattern=task.endpoint_pattern,
            model_pattern=task.model_pattern,
        )

    def _generate_page_tasks(self, task: SearchTask, total: int, per_page: int) -> List[SearchTask]:
        """Generate all pagination tasks (pre-change behavior for off/web)."""
        # Limit max pages
        max_pages = min(
            math.ceil(total / per_page),
            API_MAX_PAGES if task.use_api else WEB_MAX_PAGES,
        )

        return [self._generate_page_task(task, page) for page in range(2, max_pages + 1)]

    @handle_exceptions(default_result=[], log_level="error")
    def _extract_keys_from_content(self, content: str, task: SearchTask) -> List[Service]:
        """Extract keys directly from search content"""
        services = client.collect(
            key_pattern=task.regex,
            address_pattern=task.address_pattern,
            endpoint_pattern=task.endpoint_pattern,
            model_pattern=task.model_pattern,
            text=content,
        )

        return services


@register_stage(
    name=PipelineStage.GATHER.value,
    depends_on=[PipelineStage.SEARCH.value],
    produces_for=[PipelineStage.CHECK.value],
    description="Gather keys from discovered URLs",
)
class AcquisitionStage(BasePipelineStage):
    """Pipeline stage for acquiring keys from URLs with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.GATHER.value, resources, handler, **kwargs)
        # Coverage identity is computed once per distinct pattern set (design D5).
        self._patterns_hashes: Dict[Tuple[str, str, str, str], str] = {}

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        acquisition_task = task if isinstance(task, AcquisitionTask) else AcquisitionTask()
        return f"{PipelineStage.GATHER.value}:{task.provider}:{acquisition_task.url}"

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is an AcquisitionTask."""
        return isinstance(task, AcquisitionTask)

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute acquisition task processing."""
        return self._acquisition_worker(task)

    def _acquisition_worker(self, task: AcquisitionTask) -> Optional[StageOutput]:
        """Pure functional acquisition worker implementation"""
        mode = self._failure_handling_mode()
        try:
            # Execute acquisition using global collect function
            metadata: Dict[str, Any] = {}
            try:
                services = client.collect(
                    key_pattern=task.key_pattern,
                    url=task.url,
                    retries=task.retries,
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                    metadata=metadata,
                )
            except TransientFetchError as e:
                if mode == "strict":
                    # The blob fetch never happened: record a failed gather (no
                    # coverage row) and propagate for bounded requeue (design D4).
                    self._record_gathered(task, success=False)
                    raise
                # legacy/shadow preserve the pre-change outcome (empty success);
                # shadow additionally counts and logs the failure-empty.
                if mode == "shadow":
                    self._detect_failure_empty(task, e)
                services = []

            # Lazily enrich the gathered repository's metadata (cache-first;
            # fail-open) so link rows carry repository push evidence.
            _refresh_repo_meta(self.resources, [task.url])

            # Create output object
            output = StageOutput(task=task)

            # Carry the gathered page's file-commit date to the registry writer
            # and feed the web fill-rate drift monitor.
            file_commit_date = metadata.get("file_commit_date")
            output.add_link_metadata(
                task.provider,
                {task.url: LinkMetadata(file_commit_date=file_commit_date, transport=self._transport(task))},
            )
            date_metrics = getattr(self.resources, "date_metrics", None)
            if date_metrics is not None:
                date_metrics.record_web(file_commit_date is not None)

            # Create check tasks for found services
            if services:
                source_hash = url_hash(task.url)
                for service in services:
                    check_task = TaskFactory.create_check_task(task.provider, service, source_url_hash=source_hash)
                    output.add_task(check_task, PipelineStage.CHECK.value)

                # Add material keys to be saved
                output.add_result(task.provider, ResultType.MATERIAL.value, services)

            # Add the processed link to be saved
            output.add_links(task.provider, [task.url])

            # Write-only hook: record successful gather + coverage
            self._record_gathered(task, success=True)

            return output

        except TransientFetchError:
            # Strict mode: propagate to ``process_task`` for mode-policy handling
            # (the failed gather was already recorded above).
            raise

        except Exception as e:
            # Write-only hook: record failed gather
            self._record_gathered(task, success=False)
            logger.error(f"[{self.name}] error for provider: {task.provider}, task: {task}, message: {e}")
            return None

    def _transport(self, task: ProviderTask) -> str:
        """Resolve the configured transport ("api"/"web") for a provider task."""
        config = self.resources.task_configs.get(task.provider)
        return "api" if getattr(config, "use_api", False) else "web"

    def _record_gathered(self, task: AcquisitionTask, success: bool) -> None:
        """Write-only hook: record a gather outcome (and coverage on success)."""
        registry = getattr(self.resources, "registry", None)
        if registry is None:
            return
        try:
            key = (task.key_pattern, task.address_pattern, task.endpoint_pattern, task.model_pattern)
            digest = self._patterns_hashes.get(key)
            if digest is None:
                digest = patterns_hash(
                    key_pattern=task.key_pattern,
                    address_pattern=task.address_pattern,
                    endpoint_pattern=task.endpoint_pattern,
                    model_pattern=task.model_pattern,
                )
                self._patterns_hashes[key] = digest
            transport = self._transport(task)
            registry.record_gather(
                task.url,
                provider=task.provider,
                patterns_hash=digest,
                success=success,
                transport=transport,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[{self.name}] registry gather hook failed: {e}")


@register_stage(
    name=PipelineStage.CHECK.value,
    depends_on=[],
    produces_for=[PipelineStage.INSPECT.value],
    description="Validate API keys",
)
class CheckStage(BasePipelineStage):
    """Pipeline stage for validating API keys with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.CHECK.value, resources, handler, **kwargs)

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        check_task = task if isinstance(task, CheckTask) else None
        if check_task and check_task.service:
            service = check_task.service
            # Hash the secret: task ids are rendered in log lines.
            digest = key_hash(task.provider, service.key, service.address, service.endpoint)
            return f"{PipelineStage.CHECK.value}:{task.provider}:{digest}"

        return f"{PipelineStage.CHECK.value}:{task.provider}:unknown"

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is a CheckTask."""
        return isinstance(task, CheckTask)

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute check task processing."""
        return self._check_worker(task)

    def _check_worker(self, task: CheckTask) -> Optional[StageOutput]:
        """Pure functional check worker implementation"""
        try:
            # Get provider instance
            provider = self.resources.providers.get(task.provider)
            if not provider or not isinstance(provider, IProvider):
                logger.error(f"[{self.name}] unknown provider: {task.provider}, type: {type(provider)}")
                return None

            service = task.service
            effective_address = task.custom_url or service.address
            digest = key_hash(task.provider, service.key, effective_address, service.endpoint)
            source_url_hash = getattr(task, "source_url_hash", "") or ""

            # Ledger consultation (add-key-ledger D4): a known, fresh key skips
            # the provider call entirely.  Fail-open: any ledger problem falls
            # through to a normal check.
            if self._should_skip_check(task, service, effective_address, digest, source_url_hash):
                return self._skip_output(task, digest)

            # Apply rate limiting
            service_type = get_service_name(task.provider)
            if not self.resources.limiter.acquire(service_type):
                wait_time = self.resources.limiter.wait_time(service_type)
                if wait_time > 0:
                    time.sleep(wait_time)
                    if not self.resources.limiter.acquire(service_type):
                        bucket = self.resources.limiter._get_bucket(service_type)
                        max_value = bucket.burst if bucket else "unknown"
                        logger.info(
                            f"[{self.name}] rate limit exceeded for provider: {task.provider}, max: {max_value}"
                        )
                        # Limiter starvation is a failure-empty: requeue instead
                        # of silently dropping the key's validation (design D5).
                        raise TransientFetchError(
                            f"provider limiter starved for provider: {task.provider}"
                        )

            # Execute check
            result = provider.check(
                token=task.service.key,
                address=effective_address,
                endpoint=task.service.endpoint,
                model=task.service.model,
            )

            # Report rate limit success
            self.resources.limiter.report_result(service_type, True)

            # Persist the outcome to the ledger (hash/mask only; fail-open).
            self._record_key_outcome(task, service, effective_address, digest, source_url_hash, result)

            # Create output object
            output = StageOutput(task=task)

            # Handle result based on availability
            if result.available:
                # Create inspect task
                inspect_task = TaskFactory.create_inspect_task(task.provider, task.service)
                output.add_task(inspect_task, PipelineStage.INSPECT.value)

                # Add valid key to be saved
                output.add_result(task.provider, ResultType.VALID.value, [task.service])

            else:
                # Categorize based on error reason
                if result.reason == ErrorReason.NO_QUOTA:
                    output.add_result(task.provider, ResultType.NO_QUOTA.value, [task.service])

                elif result.reason in [
                    ErrorReason.RATE_LIMITED,
                    ErrorReason.NO_MODEL,
                    ErrorReason.NO_ACCESS,
                ]:
                    output.add_result(task.provider, ResultType.WAIT_CHECK.value, [task.service])

                else:
                    output.add_result(task.provider, ResultType.INVALID.value, [task.service])

            return output

        except TransientFetchError:
            # Limiter starvation: propagate so ``process_task`` applies the mode
            # policy (strict requeues).  No provider result to report.
            raise

        except Exception as e:
            # Report rate limit failure
            self.resources.limiter.report_result(get_service_name(task.provider), False)
            logger.error(f"[{self.name}] error for provider: {task.provider}, task: {task}, message: {e}")

            return None

    def _should_skip_check(
        self, task: CheckTask, service: Service, effective_address: str, digest: str, source_url_hash: str
    ) -> bool:
        """Consult the ledger for a fresh-skip decision (fail-open in the check direction).

        ``KeyLedger.decide`` is batch-ready, but the worker loop processes one
        task per iteration, so the batch is size 1 here (design D4 note): one
        indexed point lookup per check, no extra reads.
        """
        ledger = getattr(self.resources, "key_ledger", None)
        if ledger is None or not getattr(ledger, "enabled", False):
            return False
        try:
            decisions = ledger.decide(
                [
                    KeyCheckRequest(
                        provider=task.provider,
                        key=service.key,
                        address=effective_address,
                        endpoint=service.endpoint,
                        source_url_hash=source_url_hash,
                    )
                ]
            )
        except Exception as e:  # defensive: decide itself fails open
            logger.warning(f"[{self.name}] key-ledger consultation failed (fail-open): {e}")
            return False
        decision = decisions[0] if decisions else None
        return bool(decision is not None and decision.skip)

    def _skip_output(self, task: CheckTask, digest: str) -> StageOutput:
        """Emit an observation-only output (no results => no shard append)."""
        registry = getattr(self.resources, "registry", None)
        if registry is not None:
            try:
                registry.record_key_observation(digest)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"[{self.name}] ledger observation hook failed: {e}")
        return StageOutput(task=task)

    def _record_key_outcome(
        self,
        task: CheckTask,
        service: Service,
        effective_address: str,
        digest: str,
        source_url_hash: str,
        result: Any,
    ) -> None:
        """Write the check outcome to the ledger (fail-open; hash/mask only)."""
        registry = getattr(self.resources, "registry", None)
        if registry is None:
            return
        status = self._result_status(result)
        try:
            registry.record_key(
                digest,
                provider=task.provider,
                key_ref_masked=mask_key(service.key),
                address=effective_address,
                endpoint=service.endpoint,
                status=status,
                source_url_hash=source_url_hash,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[{self.name}] ledger upsert hook failed: {e}")

    @staticmethod
    def _result_status(result: Any) -> str:
        """Map a CheckResult to the ledger status vocabulary."""
        if result.available:
            return ResultType.VALID.value
        if result.reason == ErrorReason.NO_QUOTA:
            return ResultType.NO_QUOTA.value
        if result.reason in (ErrorReason.RATE_LIMITED, ErrorReason.NO_MODEL, ErrorReason.NO_ACCESS):
            return ResultType.WAIT_CHECK.value
        return ResultType.INVALID.value


@register_stage(
    name=PipelineStage.INSPECT.value,
    depends_on=[],
    produces_for=[],
    description="Inspect API capabilities for validated keys",
)
class InspectStage(BasePipelineStage):
    """Pipeline stage for inspecting API capabilities with pure functional processing"""

    def __init__(self, resources: StageResources, handler: OutputHandler, **kwargs):
        super().__init__(PipelineStage.INSPECT.value, resources, handler, **kwargs)

    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        inspect_task = task if isinstance(task, InspectTask) else None
        if inspect_task and inspect_task.service:
            service = inspect_task.service
            # Hash the secret: task ids are rendered in log lines.
            digest = key_hash(task.provider, service.key, service.address, service.endpoint)
            return f"{PipelineStage.INSPECT.value}:{task.provider}:{digest}"

        return f"{PipelineStage.INSPECT.value}:{task.provider}:unknown"

    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that task is an InspectTask."""
        return isinstance(task, InspectTask)

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute inspect task processing."""
        return self._inspect_worker(task)

    def _inspect_worker(self, task: InspectTask) -> Optional[StageOutput]:
        """Pure functional inspect worker implementation"""
        try:
            # Get provider instance
            provider = self.resources.providers.get(task.provider)
            if not provider or not isinstance(provider, IProvider):
                logger.error(f"[{self.name}] unknown provider: {task.provider}, type: {type(provider)}")
                return None

            # Get model list
            models = provider.inspect(
                token=task.service.key, address=task.service.address, endpoint=task.service.endpoint
            )

            # Create output object
            output = StageOutput(task=task)

            # Add models to be saved
            if models:
                output.add_models(task.provider, task.service.key, models)

            return output

        except TransientFetchError:
            # Uniform contract (design D8): typed transient failures are handled
            # by the process_task mode policy, not swallowed here.
            raise

        except Exception as e:
            logger.error(f"[{self.name}] inspect models error, provider: {task.provider}, task: {task}, message: {e}")
            return None
