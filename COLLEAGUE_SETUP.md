# 同仁帳號 Backfill Setup（10 分鐘搞定）

## 一次性設定

1. **Fork 這個 repo 到你自己的 GH 帳號**
   - 開 https://github.com/ailifelabtw/data-fetch-runner
   - 右上角點 **Fork** → 確認

2. **設兩個 secrets**
   - 進你 fork 的 repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
   - 加 secret：
     - Name: `API_ENDPOINT`
     - Value: `https://tender-award-backfill.press-093.workers.dev`
   - 再加一個：
     - Name: `API_TOKEN`
     - Value: （從專案管理者拿 WORKER_API_TOKEN）

3. **檢查 fork 是不是最新版**
   - 進 fork repo 首頁
   - 如果看到上方寫 "This branch is X commits behind" → 點 **Sync fork** → **Update branch**

## 日常使用：啟動 backfill 細爬

進你 fork 的 **Actions** tab → 左側選 **Backfill Detail Scrape** → 右邊 **Run workflow** 按鈕

預設參數即可：
- parallel: 15（同時開 15 個 runner）
- batch_size: 20
- max_iterations: 5

點 **Run workflow** → 一輪約 30-60 分鐘跑完約 1500 個 detail。

## 自動排程

`backfill.yml` 已內建 cron 每 30 分鐘自動 fire，所以**設好 fork + secrets 就會自動跑**，不用一直手動點。

要關掉自動：editing `.github/workflows/backfill.yml`，把 `schedule:` 那兩行 comment 掉再 push。

## 兩條線的隔離

- `Daily` workflow（owner 帳號跑）：claim daily 新標案（`source_filter=daily`，自動排除 backfill）
- `Backfill Detail Scrape`（你跑）：claim backfill task（`source_filter=backfill`，只看 backfill source）

CF Worker 兩種 claim 互不干擾，可以同時跑。

## 看進度

owner 那邊有監控腳本，或你可以直接 curl：
```bash
curl -H "Authorization: Bearer $API_TOKEN" \
  "https://tender-award-backfill.press-093.workers.dev/daily-status"
```
