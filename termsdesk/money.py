"""Indian rupee formatting: 1800000 -> 'Rs 18 lakh', 15000000 -> 'Rs 1.5 crore', 250000 -> 'Rs 2.5 lakh'."""


def group_indian(n: int) -> str:
    s = str(abs(int(n)))
    if len(s) <= 3:
        out = s
    else:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + out


def inr(n: int | None) -> str:
    if n is None:
        return "not stated"
    n = int(n)
    if n >= 10_000_000 and n % 100_000 == 0:
        v = n / 10_000_000
        return f"Rs {v:g} crore"
    if n >= 100_000 and n % 1_000 == 0:
        v = n / 100_000
        return f"Rs {v:g} lakh"
    return f"Rs {group_indian(n)}"
