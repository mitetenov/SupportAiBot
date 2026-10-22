# Immutable scorer contracts

`scorer_baseline.json` contains 146 distinct synthetic inputs and exact outputs
captured while the 59 original evaluator tests passed against source commit
`45fb11a` on 2026-10-23. No real provider, user data or credentials were used.
`TestFrozenScorerContracts` compares every verdict, tool sequence, response and
violation string. The rules and synthetic MCP defaults are declarative JSON;
all execution is native Go. Source used for capture remains in Git history.

Existing provider/configuration/runtime/SQL fixture files in sibling packages
retain their original provenance and filenames. They are data, require no
retired interpreter, and must not be regenerated from the implementation under
test merely to make a failure green.
