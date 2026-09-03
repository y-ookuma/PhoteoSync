# PhoteoSync — JMA GitHub Actions構成

1. `index.html` をリポジトリのトップへ配置。
2. `update_jma_history.py` をトップへ配置。
3. `.github/workflows/jma-history.yml` をそのまま配置。
4. `data/jma-history.json` を配置。
5. GitHub Actions の **Update JMA history data** を `Run workflow` で初回実行。
6. 成功すると `data/jma-history.json` が更新され、GitHub Pages の `index.html` がそのJSONを読み込みます。

ブラウザから気象庁 obsdl へ直接POSTしたり、CORSプロキシを使用したりしません。
