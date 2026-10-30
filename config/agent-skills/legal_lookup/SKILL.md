---
name: chinese-law-verifier
description: 用法规全称和条号取得中国法原文、定位及实际来源，按需要补查官方出处与适用版本。
metadata:
  tea-skill-id: legal_lookup
  source-author: 李伯阳律师
---
# 中国法原文查询与一般法规定位

中国法精确查询方法来自用户提供的 chinese-law-verifier/SKILL.md。通过 lookup_chinese_law 提供法规标准全称和条号，一次最多四条。阿拉伯条号由程序转换为中文条号；不要猜法规全称或伪造条文。

默认先读内置库的同名同条完整原文，缺项调用 https://lawapi.lslby.com/api/v1/article。source=api 用于明确要求核对该公益接口的情况。接口只接收法名与条号，不接收交易、合同、当事人及其他私人事实。接口请求共用6秒截止时间，失败后保留内置匹配结果；缺项明确报告，不断言相关制度不存在。

公益API由李伯阳律师提供，属于第三方来源。返回成功仅证明取得匹配的条文文本；接口没有提供的公布日期、施行日期或修订关系仍是未知，不宣称来自官方数据库或已经确认现行有效。需要官方出处、历史或未来适用版本时，继续用 search_legal_evidence、get_law_versions 或 web_search 定位，再用 read_law 取得官方正文。

未知条号和其他法域优先使用 search_legal_evidence 查询内部库，按真实记录编号 read_law，版本关系用 get_law_versions。内库缺少相关原文或适用版本再用 web_search 找官方原件，以 read_law 读取当前支持法域的官方页面。保留完整条文中的主体、适用范围和例外；摘要不能冒充原文。最终直接提供法名、原文、条款位置、实际来源链接和必要的版本限制，不扩展为完整合规报告。

本入口融合 sgcite 与 bart-statutory-reference-checker 的新加坡引用核对方法。涉及新加坡案件真实性、当事人、段落引文或法规修订时，用 load_skill(name="legal_lookup", resource="references/singapore.md") 读取方法与当前工具限制。未安装上游 CLI、Word 插件或私有 BART 后端，不能宣称这些服务已经运行。核对引用是否存在与判断其是否支持法律命题分别处理；后一项按需读取 legal 的 references/research.md。
