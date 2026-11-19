"""Discovery rules: ordered global rules that classify discovery scan results.

* ``facts``       the typed facts a condition can test
* ``conditions``  condition trees: validation and a pure evaluator
* ``actions``     the action catalog (import decision, group, template, tags, ...)
* ``engine``      ``evaluate(facts, rules) -> plan`` (pure) and dispositions
* ``runner``      settings, rule loading, preview and evaluation of a scan

See ``docs/discovery-rule-engine.md``.
"""
