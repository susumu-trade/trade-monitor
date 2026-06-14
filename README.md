# trade-monitor (cloud)

DMM CFD 個人向けトレード監視のクラウド版。GitHub Actions の cron（5分ごと）で実行し、
急変動・ニュース・経済指標カレンダー・売買シグナルを Telegram へ通知する。

- 外部ライブラリ不要（Python標準ライブラリのみ）
- 秘密情報はリポジトリに置かない。GitHub Secrets に設定:
  - `TG_BOT_TOKEN` … Telegram Botトークン
  - `TG_CHAT_ID` … 送信先 chat_id
- 設定: `cloud_config.json`（しきい値・銘柄・フィード・指標パラメータ）
- 状態: `state/state.json`（既読ニュース・クールダウン等。ワークフローが自動保存）

手動テスト: リポジトリの Actions タブ → trade-monitor → Run workflow
