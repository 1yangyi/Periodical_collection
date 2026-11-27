# 期刊投稿格式采集实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** 按单刊或 Excel 清单采集官方公开投稿格式文件、网页正文和来源记录。

**Architecture:** HTTP 发现和文件校验构成主流程。HTML 识别、浏览器补查、结果存储分开测试，共享单刊入口。

**Tech Stack:** Python、requests、Beautiful Soup、openpyxl，按需接入 Playwright 和 Browser Use。

**Spec:** `docs/期刊投稿格式文件采集需求.md`

## Global Constraints

仅访问公开资料。保存原件和来源。没有经过验证的相关 Word/PDF 原件时，状态必须为 uncompleted。仅网页公开时保存期刊名投稿格式.md。

## Review Focus

验证网页正文保留列表和表格、同一附件多来源、错误页冒充附件、访问范围上限和批量失败后的继续运行。

## 实施任务

- [x] 实现并测试 HTML 链接的分类、优先级与正文转 Markdown。
- [x] 实现受范围限制的 HTTP、Playwright 和 Browser Use 补查，记录未解决线索。
- [x] 实现附件验证、来源合并、网页快照、单刊和 Excel 批量命令。
- [x] 用本地集成测试验证典型失败和完成条件，并用已知期刊验证公开入口。Browser Use Cloud 使用模拟测试。
