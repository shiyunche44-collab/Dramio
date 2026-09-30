.PHONY: arch-check drama-ir-check

# 架构检查：先自测规则，再检查整个仓库（docs/governance.md §5）
arch-check:
	python3 -m unittest discover -s tools/archcheck -p "test_*.py"
	python3 -m unittest discover -s tools/workflow -p "test_*.py"
	python3 tools/archcheck/archcheck.py

# DramaIR：单元测试 + 标准样例严格校验（packages/drama-ir/README.md）
drama-ir-check:
	cd packages/drama-ir/python && python3 -m unittest discover -s tests -t .
	cd packages/drama-ir/python && python3 -m dramio_drama_ir validate --strict ../examples/v0/*.json
