.PHONY: arch-check

# 架构检查：先自测规则，再检查整个仓库（docs/governance.md §5）
arch-check:
	python3 -m unittest discover -s tools/archcheck -p "test_*.py"
	python3 tools/archcheck/archcheck.py
