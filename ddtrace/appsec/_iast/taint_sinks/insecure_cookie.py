from typing import Dict
from typing import Optional

from ..._constants import IAST_SPAN_TAGS
from .. import _is_iast_enabled
from .. import oce
from .._iast_request_context import is_iast_request_enabled
from .._metrics import _set_metric_iast_executed_sink
from .._metrics import increment_iast_span_metric
from .._taint_tracking import iast_taint_log_error
from ..constants import VULN_INSECURE_COOKIE
from ..constants import VULN_NO_HTTPONLY_COOKIE
from ..constants import VULN_NO_SAMESITE_COOKIE
from ..taint_sinks._base import VulnerabilityBase


@oce.register
class InsecureCookie(VulnerabilityBase):
    vulnerability_type = VULN_INSECURE_COOKIE
    scrub_evidence = False
    skip_location = True


@oce.register
class NoHttpOnlyCookie(VulnerabilityBase):
    vulnerability_type = VULN_NO_HTTPONLY_COOKIE
    skip_location = True


@oce.register
class NoSameSite(VulnerabilityBase):
    vulnerability_type = VULN_NO_SAMESITE_COOKIE
    skip_location = True


def asm_check_cookies(cookies: Optional[Dict[str, str]]) -> None:
    if not cookies:
        return
    if _is_iast_enabled() and is_iast_request_enabled():
        try:
            for cookie_key, cookie_value in cookies.items():
                lvalue = cookie_value.lower().replace(" ", "")
                # If lvalue starts with ";" means that the cookie is empty, like ';httponly;path=/;samesite=strict'
                if lvalue == "" or lvalue.startswith(";") or lvalue.startswith('""'):
                    continue

                if ";secure" not in lvalue:
                    increment_iast_span_metric(
                        IAST_SPAN_TAGS.TELEMETRY_EXECUTED_SINK, InsecureCookie.vulnerability_type
                    )
                    _set_metric_iast_executed_sink(InsecureCookie.vulnerability_type)
                    InsecureCookie.report(evidence_value=cookie_key)

                if ";httponly" not in lvalue:
                    increment_iast_span_metric(
                        IAST_SPAN_TAGS.TELEMETRY_EXECUTED_SINK, NoHttpOnlyCookie.vulnerability_type
                    )
                    _set_metric_iast_executed_sink(NoHttpOnlyCookie.vulnerability_type)
                    NoHttpOnlyCookie.report(evidence_value=cookie_key)

                if ";samesite=" in lvalue:
                    ss_tokens = lvalue.split(";samesite=")
                    if len(ss_tokens) <= 1:
                        report_samesite = True
                    else:
                        ss_tokens[1] = ss_tokens[1].lower()
                        if ss_tokens[1].startswith("strict") or ss_tokens[1].startswith("lax"):
                            report_samesite = False
                        else:
                            report_samesite = True
                else:
                    report_samesite = True

                if report_samesite:
                    increment_iast_span_metric(IAST_SPAN_TAGS.TELEMETRY_EXECUTED_SINK, NoSameSite.vulnerability_type)
                    _set_metric_iast_executed_sink(NoSameSite.vulnerability_type)
                    NoSameSite.report(evidence_value=cookie_key)
        except Exception as e:
            iast_taint_log_error("[IAST] error in asm_check_cookies. {}".format(e))
