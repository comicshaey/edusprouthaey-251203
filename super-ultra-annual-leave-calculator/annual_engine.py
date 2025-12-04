# annual_engine.py
# 브라우저(Pyodide) / 로컬 / 서버 어디서나 돌릴 수 있게
# 순수 파이썬으로만 작성한 연차·복무·수당 엔진

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from math import floor

# -----------------------------
# 0. 기본 데이터 구조
# -----------------------------


@dataclass
class ServiceProfile:
    """근속·출근 정보 요약"""

    hire_date: str  # "YYYY-MM-DD"
    base_date: str  # "YYYY-MM-DD"
    service_years: float  # 기준일 현재 근속연수(년 단위)
    full_years: int
    attendance_rate: float  # 출근율(%)
    full_months: int  # 개근 개월 수


@dataclass
class WageProfile:
    """통상임금 계산에 필요한 정보"""

    wage_type: str  # "hourly" | "daily" | "monthly"
    wage_amount: float  # 시급/일급/월급
    hours_per_day: float
    monthly_work_days: float

    def daily_wage(self) -> float:
        """1일 통상임금(원단위, 이론값)"""
        if self.wage_type == "hourly":
            if self.wage_amount > 0 and self.hours_per_day > 0:
                return self.wage_amount * self.hours_per_day
            return 0.0
        if self.wage_type == "daily":
            return self.wage_amount
        if self.wage_type == "monthly":
            if self.wage_amount > 0 and self.monthly_work_days > 0:
                return self.wage_amount / self.monthly_work_days
            return 0.0
        return 0.0


@dataclass
class RuleProfile:
    """규정 세트(단체협약/지침/법정 기본 등)"""

    id: str
    name: str
    grant_type: str  # "law_basic" | "gw_cba_school" | "gw_cba_institute" | "external_days" | "manual_only"
    first_year_max: int = 11  # 1년 미만 월 개근 최대 일수
    default_days_after_1y: int = 26  # 1년 이상 기본 연차(예시값)
    money_step: int = 10  # 금액 절사 단위(기본 10원)
    money_mode: str = "floor"  # "floor" | "round" | "ceil"


# -----------------------------
# 1. 나이스 근무상황목록 레코드 파싱
# -----------------------------


@dataclass
class NiceRecord:
    """나이스 근무상황목록 한 줄"""

    leave_type: str  # 종별(나이스 원문)
    duration_raw: str  # "0일 6시간 30분" 같은 문자열
    hours_per_day: float = 8.0  # 1일 소정근로시간(기본 8시간으로 가정)

    def parse_duration(self) -> Dict[str, float]:
        """
        "0일 6시간 30분" → 총 분 단위로 환산하고,
        일/시/분/총시간(10진법)까지 같이 반환
        """
        txt = (self.duration_raw or "").strip()
        if not txt:
            return {
                "days": 0,
                "hours": 0,
                "minutes": 0,
                "total_minutes": 0,
                "total_hours": 0.0,
            }

        import re

        nums = re.findall(r"\d+", txt)
        d = h = m = 0
        if len(nums) >= 3:
            d, h, m = map(int, nums[:3])
        elif len(nums) == 2:
            d, h = map(int, nums)
        elif len(nums) == 1:
            # 애매하면 일수로 본다 (나이스 포맷 기준 대체로 안전)
            d = int(nums[0])

        hpd = self.hours_per_day or 8
        total_minutes = (d * hpd + h) * 60 + m
        total_hours = total_minutes / 60.0
        return {
            "days": d,
            "hours": h,
            "minutes": m,
            "total_minutes": total_minutes,
            "total_hours": total_hours,
        }


def summarize_nice_records(
    records: List[NiceRecord],
) -> List[Dict[str, Any]]:
    """
    나이스 레코드 리스트 → 종별별 집계 결과.
    JS에서 이미 엑셀을 파싱해서 {leave_type, duration_raw} 형태로 넘겨주면
    브라우저(Pyodide)에서도 그대로 사용할 수 있다.
    """
    by_type: Dict[str, Dict[str, Any]] = {}

    for rec in records:
        parsed = rec.parse_duration()
        key = rec.leave_type.strip() or "미지정"
        if key not in by_type:
            by_type[key] = {
                "leave_type": key,
                "count": 0,
                "total_minutes": 0,
                "hours_per_day": rec.hours_per_day or 8.0,
            }
        by_type[key]["count"] += 1
        by_type[key]["total_minutes"] += parsed["total_minutes"]

    results: List[Dict[str, Any]] = []
    for key, info in sorted(by_type.items(), key=lambda x: x[0]):
        total_minutes = info["total_minutes"]
        hpd = info["hours_per_day"]
        total_hours = total_minutes / 60.0

        # 일/시/분 형태
        day_from_min = total_minutes // int(hpd * 60)
        remain_min = total_minutes - day_from_min * hpd * 60
        hour_from_min = remain_min // 60
        minute = remain_min - hour_from_min * 60

        # 10진법 시간
        decimal_hours = round(total_hours, 1)
        days_from_hours = int(decimal_hours // hpd)
        remain_hours_dec = round(decimal_hours - days_from_hours * hpd, 1)

        results.append(
            {
                "leave_type": key,
                "count": info["count"],
                "sum_d_h_m": f"{int(day_from_min)}일 {int(hour_from_min)}시간 {int(minute)}분",
                "sum_hours_decimal": decimal_hours,
                "converted_days_hours": f"{days_from_hours}일 {remain_hours_dec:.1f}시간",
            }
        )

    return results


# -----------------------------
# 2. 연차 부여일수 추천 로직
# -----------------------------


def suggest_annual_days(
    rule: RuleProfile, svc: ServiceProfile
) -> Dict[str, Any]:
    """
    규정 세트 + 근속/출근 요약 정보 → 추천 연차일수와 설명.
    JS 버전 suggestAnnualDays()와 같은 역할을 파이썬으로 옮긴 것.
    """

    full_years = svc.full_years
    rate = svc.attendance_rate
    full_months = svc.full_months

    suggested: Optional[float] = 0.0
    desc = ""

    if rule.grant_type == "law_basic":
        if full_years < 1:
            suggested = min(full_months, rule.first_year_max)
            desc = (
                f"법정 기본형: 1년 미만, 월 개근 {full_months}개월 → "
                f"{suggested}일 (최대 {rule.first_year_max}일 예시)"
            )
        else:
            if rate < 80:
                suggested = float(full_months)
                desc = (
                    f"법정 기본형: 출근율 {rate:.1f}% (80% 미만) → "
                    f"월 개근 {full_months}개월 = {suggested}일"
                )
            else:
                extra = max(0, min(10, (full_years - 1) // 2))
                suggested = 15 + extra
                desc = (
                    f"법정 기본형: 근속 {full_years}년 / 출근율 {rate:.1f}% → "
                    f"기본 15일 + 가산 {extra}일 = {suggested}일 (예시)"
                )

    elif rule.grant_type in ("gw_cba_school", "gw_cba_institute"):
        if full_years < 1:
            suggested = float(min(full_months, rule.first_year_max))
            desc = (
                f"강원 CBA 샘플: 1년 미만, 월 개근 {full_months}개월 → "
                f"{suggested}일 (최대 {rule.first_year_max}일 예시)"
            )
        else:
            if rate >= 80:
                suggested = float(rule.default_days_after_1y)
                desc = (
                    f"강원 CBA 샘플: 근속 {full_years}년 / 출근율 {rate:.1f}% → "
                    f"{suggested}일 (grant_rules.default_days_after_1y 예시 값)"
                )
            else:
                suggested = float(full_months)
                desc = (
                    f"강원 CBA 샘플: 출근율 {rate:.1f}% (80% 미만) → "
                    f"월 개근 {full_months}개월 = {suggested}일"
                )

    elif rule.grant_type == "external_days":
        suggested = None
        desc = (
            "통상임금 지침 반영형: 연차일수는 다른 모듈에서 계산한 값을 그대로 쓰고, "
            "이 모듈에서는 수당만 계산합니다."
        )

    elif rule.grant_type == "manual_only":
        suggested = None
        desc = (
            "커스텀: 부여 연차일수는 사용자가 직접 입력해야 합니다."
        )

    return {
        "suggested_days": suggested,
        "description": desc,
    }


# -----------------------------
# 3. 미사용 연차수당 계산
# -----------------------------


def round_money(amount: float, step: int = 10, mode: str = "floor") -> float:
    """
    금액 절사/반올림/올림.
    기본은 10원 단위 버림 → 1원 단위는 항상 날아감.
    """
    if amount <= 0:
        return 0.0

    if step is None or step < 1:
        step = 1

    base = amount / step
    if mode == "round":
        from math import floor as _floor

        # 파이썬 round 오동작 방지용
        tmp = base + 1e-8
        base_rounded = int(tmp + 0.5)
        result = base_rounded * step
    elif mode == "ceil":
        from math import ceil

        result = ceil(base) * step
    else:
        # 기본 floor
        result = floor(base) * step

    return float(result)


def calc_unused_leave_payout(
    rule: RuleProfile,
    granted_days: float,
    used_days: float,
    wage_profile: WageProfile,
) -> Dict[str, Any]:
    """
    부여 연차일수 / 사용 연차일수 / 임금 정보를 받아
    미사용 연차수당 계산. 결과는 금액 원단위와 절사된 금액 둘 다 반환.
    """

    if granted_days < 0:
        granted_days = 0.0
    if used_days < 0:
        used_days = 0.0

    unused = granted_days - used_days
    if unused < 0:
        unused = 0.0

    daily = wage_profile.daily_wage()
    payout_raw = unused * daily

    # 규정 세트에 지정된 절사 규칙(기본 10원 버림)
    step = rule.money_step or 10
    mode = rule.money_mode or "floor"

    payout_rounded = round_money(payout_raw, step=step, mode=mode)

    return {
        "granted_days": granted_days,
        "used_days": used_days,
        "unused_days": unused,
        "daily_wage_raw": daily,
        "payout_raw": payout_raw,
        "payout_rounded": payout_rounded,
        "rounding_step": step,
        "rounding_mode": mode,
    }


# -----------------------------
# 4. 한 번에 묶어서 돌리는 헬퍼
# -----------------------------


def full_pipeline(
    rule_id: str,
    service_dict: Dict[str, Any],
    wage_dict: Dict[str, Any],
    granted_days: float,
    used_days: float,
) -> Dict[str, Any]:
    """
    JS / PyScript에서 쓰기 편하게 만든 통합 함수.
    - 규정 세트 선택
    - 근속·출근 정보 파싱
    - 연차 추천
    - 미사용수당 계산
    을 한 번에 돌려서 딕셔너리로 반환.
    """

    rule_map = {
      "law_basic": RuleProfile(id="law_basic", name="법정 기본형",
                               grant_type="law_basic"),
      "gw_school_cba": RuleProfile(id="gw_school_cba", name="학교근무자 CBA 예시",
                                   grant_type="gw_cba_school"),
      "gw_institute_cba": RuleProfile(id="gw_institute_cba", name="기관근무자 CBA 예시",
                                      grant_type="gw_cba_institute"),
      "gw_wage_guideline": RuleProfile(id="gw_wage_guideline", name="통상임금 지침형",
                                       grant_type="external_days"),
      "custom": RuleProfile(id="custom", name="커스텀", grant_type="manual_only",
                            money_step=10, money_mode="floor"),
    }

    rule = rule_map.get(rule_id, rule_map["law_basic"])

    svc = ServiceProfile(
        hire_date=service_dict.get("hire_date", ""),
        base_date=service_dict.get("base_date", ""),
        service_years=float(service_dict.get("service_years", 0.0)),
        full_years=int(service_dict.get("full_years", 0)),
        attendance_rate=float(service_dict.get("attendance_rate", 0.0)),
        full_months=int(service_dict.get("full_months", 0)),
    )

    wage = WageProfile(
        wage_type=wage_dict.get("wage_type", "hourly"),
        wage_amount=float(wage_dict.get("wage_amount", 0.0)),
        hours_per_day=float(wage_dict.get("hours_per_day", 0.0)),
        monthly_work_days=float(wage_dict.get("monthly_work_days", 0.0)),
    )

    suggestion = suggest_annual_days(rule, svc)
    payout = calc_unused_leave_payout(rule, granted_days, used_days, wage)

    return {
        "rule": rule.__dict__,
        "service": svc.__dict__,
        "wage": {
            **wage.__dict__,
            "daily_wage": wage.daily_wage(),
        },
        "suggestion": suggestion,
        "payout": payout,
    }
