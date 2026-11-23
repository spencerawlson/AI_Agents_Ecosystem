"""Audience intelligence: who a business's customers are, and where.

Pure logic, no dependency on ``ventoryn`` (the SaaS layer stores the
results and runs the work as a customer job). Every module here keeps
the same rules:

- Every fact carries its evidence: the page URL and the quoted text it
  came from. A value with no evidence is not produced.
- Facts, derived values and AI hypotheses are labelled as such and
  never mixed: an AI hypothesis cites the facts it rests on.
- Text from the internet is untrusted (``untrusted.py``): it is cleaned,
  fenced as data before it reaches a model, and model output is
  validated before it is kept.
- Nothing is estimated that cannot be measured: a missing number stays
  missing.

Modules:
    untrusted   cleaning, injection flags, data fences, output checks
    website     multi-page business profile with per-claim evidence
    hypotheses  AI hypotheses (segments, problems, ...) grounded in facts
"""
