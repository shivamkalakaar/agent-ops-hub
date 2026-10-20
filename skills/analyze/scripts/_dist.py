"""Internal: probability distributions in pure Python for stats.py. Not a command.

Normal, Student t, chi-square, regularized incomplete beta and gamma, their inverses, Wilson intervals,
Fisher's exact test and rank helpers. Everything here is validated against scipy in the tests
(agreement to about 1e-10 or better on the ranges that matter for real analysis).
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)
EPS = 1e-15
TINY = 1e-300


# ---------------------------------------------------------------- normal
def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / SQRT2)


def norm_sf(x: float) -> float:
    return 0.5 * math.erfc(x / SQRT2)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def norm_ppf(p: float) -> float:
    """Inverse normal CDF: Acklam's rational start, then Newton steps on erfc (full double precision)."""
    if not 0.0 < p < 1.0:
        if p == 0.0:
            return -math.inf
        if p == 1.0:
            return math.inf
        raise ValueError("p must be in [0, 1]")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)
    plow = 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    elif p > 1 - plow:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    else:
        q = p - 0.5
        r = q * q
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    for _ in range(3):                                   # Newton refinement on the tail that is small
        if x < 0:
            err = norm_cdf(x) - p
        else:
            err = (1 - p) - norm_sf(x)
        pdf = norm_pdf(x)
        if pdf <= 0:
            break
        x -= err / pdf
    return x


# ---------------------------------------------------------------- incomplete beta and gamma
def _betacf(x: float, a: float, b: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < TINY:
        d = TINY
    d = 1.0 / d
    h = d
    for m in range(1, 30000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < TINY:
            d = TINY
        c = 1.0 + aa / c
        if abs(c) < TINY:
            c = TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < TINY:
            d = TINY
        c = 1.0 + aa / c
        if abs(c) < TINY:
            c = TINY
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbt) * _betacf(x, a, b) / a
    return 1.0 - math.exp(lbt) * _betacf(1.0 - x, b, a) / b


def gammainc_lower(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x)."""
    if x <= 0.0:
        return 0.0
    if x < a + 1.0:
        ap, s, dl = a, 1.0 / a, 1.0 / a
        for _ in range(100000):
            ap += 1.0
            dl *= x / ap
            s += dl
            if abs(dl) < abs(s) * EPS:
                break
        return s * math.exp(-x + a * math.log(x) - math.lgamma(a))
    return 1.0 - gammainc_upper(a, x)


def gammainc_upper(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x)."""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        return 1.0 - gammainc_lower(a, x)
    b = x + 1.0 - a
    c = 1.0 / TINY
    d = 1.0 / b
    h = d
    for i in range(1, 100000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < TINY:
            d = TINY
        c = b + an / c
        if abs(c) < TINY:
            c = TINY
        d = 1.0 / d
        dl = d * c
        h *= dl
        if abs(dl - 1.0) < EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


# ---------------------------------------------------------------- Student t and chi-square
def t_sf2(t: float, df: float) -> float:
    """Two-sided tail probability P(|T| > |t|)."""
    if math.isinf(t):
        return 0.0
    if df > 1e7:
        return 2 * norm_sf(abs(t))
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def t_cdf(t: float, df: float) -> float:
    tail = 0.5 * t_sf2(t, df)
    return 1.0 - tail if t > 0 else tail


def t_sf(t: float, df: float) -> float:
    return 1.0 - t_cdf(t, df) if t < 0 else 0.5 * t_sf2(t, df)


def t_isf(q: float, df: float) -> float:
    """Upper-tail quantile: t such that P(T > t) = q, for 0 < q < 1."""
    if q > 0.5:
        return -t_isf(1.0 - q, df)
    if q == 0.5:
        return 0.0
    lo, hi = 0.0, max(2.0, norm_ppf(1 - q) * 2)
    while t_sf(hi, df) > q:
        hi *= 2
        if hi > 1e12:
            break
    for _ in range(300):
        mid = (lo + hi) / 2
        if t_sf(mid, df) > q:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-15 * max(1.0, hi):
            break
    return (lo + hi) / 2


def t_ppf(p: float, df: float) -> float:
    return t_isf(1.0 - p, df) if p > 0.5 else -t_isf(p, df)


def chi2_sf(x: float, df: float) -> float:
    if x <= 0:
        return 1.0
    return gammainc_upper(df / 2.0, x / 2.0)


def chi2_cdf(x: float, df: float) -> float:
    return 1.0 - chi2_sf(x, df) if x > 0 else 0.0


def chi2_isf(q: float, df: float) -> float:
    lo, hi = 0.0, max(1.0, df)
    while chi2_sf(hi, df) > q:
        hi *= 2
    for _ in range(300):
        mid = (lo + hi) / 2
        if chi2_sf(mid, df) > q:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-15 * max(1.0, hi):
            break
    return (lo + hi) / 2


# ---------------------------------------------------------------- proportions
def wilson(x: float, n: float, conf: float = 0.95):
    """Wilson score interval for a proportion."""
    if n <= 0:
        return (float("nan"), float("nan"))
    z = norm_ppf(1 - (1 - conf) / 2)
    p = x / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def newcombe(x1, n1, x2, n2, conf=0.95):
    """Newcombe hybrid-score CI for p2 - p1 (much better than the plain Wald interval)."""
    p1, p2 = x1 / n1, x2 / n2
    l1, u1 = wilson(x1, n1, conf)
    l2, u2 = wilson(x2, n2, conf)
    d = p2 - p1
    return (d - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2), d + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2))


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p-value for the 2x2 table [[a, b], [c, d]] (same rule as scipy)."""
    r1, r2, c1, n = a + b, c + d, a + c, a + b + c + d
    lo, hi = max(0, c1 - r2), min(r1, c1)

    def logp(k):
        return (math.lgamma(r1 + 1) - math.lgamma(k + 1) - math.lgamma(r1 - k + 1) + math.lgamma(r2 + 1)
                - math.lgamma(c1 - k + 1) - math.lgamma(r2 - c1 + k + 1) - (math.lgamma(n + 1) - math.lgamma(c1 + 1) - math.lgamma(n - c1 + 1)))
    p0 = logp(a)
    tot = 0.0
    for k in range(lo, hi + 1):
        lp = logp(k)
        if lp <= p0 + 1e-7:
            tot += math.exp(lp)
    return min(1.0, tot)


# ---------------------------------------------------------------- ranks
def rankdata(xs):
    """Average ranks (1-based) with ties, plus the tie-size list."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    ties = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = r
        if j > i:
            ties.append(j - i + 1)
        i = j + 1
    return ranks, ties
