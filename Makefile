.PHONY: setup run demo test review clean

PY := uv run python

setup:            ## 依存をインストール
	uv sync

run:              ## articles/ を分析して out/ に出力
	$(PY) -m pipeline.run

demo:             ## サンプル記事（架空データ）で動作確認 → out/demo/
	$(PY) -m pipeline.run --articles fixtures/sample_articles --out out/demo --config fixtures/demo.config.yaml

test:
	uv run pytest -q

review:           ## 辞書に入れる候補（頻出だが未登録の語）を頻度順に出す
	$(PY) -m pipeline.review

clean:
	rm -rf out/*.gexf out/*.html out/*.csv out/*.json out/report.md
