"""Descriptive variation and Wilson intervals; no scoring or model judgments."""
from math import isfinite, sqrt
from statistics import mean, stdev


def describe(values):
    samples = [v for v in values if v is not None]
    if any(type(v) not in (int, float) or not isfinite(v) for v in samples):
        raise ValueError("measurements must be finite numbers or null")
    samples.sort()
    n = len(samples)
    def quantile(q):
        if not n:
            return None
        position = (n - 1) * q
        low = int(position)
        high = min(low + 1, n - 1)
        return samples[low] + (samples[high] - samples[low]) * (position - low)
    return dict(samples=n, mean=mean(samples) if n else None,
                stddev=stdev(samples) if n >= 2 else None,
                median=quantile(0.5), p95=quantile(0.95),
                min=samples[0] if n else None, max=samples[-1] if n else None)


def wilson95(successes, total):
    """Two-sided 95% Wilson score interval for independent Bernoulli trials.

    NIST: https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm
    No observed trials means unknown; never manufacture a zero-width interval.
    """
    if type(successes) is not int or type(total) is not int or not 0 <= successes <= total:
        raise ValueError("invalid binomial counts")
    if total == 0:
        return None, None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    radius = z * sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return max(0.0, center-radius), min(1.0, center+radius)
