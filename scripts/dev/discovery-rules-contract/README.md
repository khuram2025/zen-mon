# Discovery rules contract vectors

`vectors.json` pins what `app.services.discovery_rules.engine.evaluate` and
`engine.disposition` decide for a set of facts and rules: which rules match,
which values win, which tags merge, which rules are shadowed and what happens
to the result under a profile import mode.

`server/tests/test_discovery_rules_engine.py::test_contract_vectors` runs every
vector. Preview, re-evaluation of a past scan and the real end-of-scan
evaluation all call the same functions, so a vector describes all three.

Add a vector when the evaluation semantics change; never edit an expectation
to make a failing test pass without changing the documented semantics in
`docs/discovery-rule-engine.md` too.
