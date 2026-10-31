# Third-party sources

## Vercel AI Elements Question

`src/components/ai-elements/question.tsx` is sourced from [vercel/ai-elements](https://github.com/vercel/ai-elements/blob/6a9d5b1822ffb10bba4bd97175f01edd7d8651cd/packages/elements/src/question.tsx), commit `6a9d5b1822ffb10bba4bd97175f01edd7d8651cd`. Copyright 2023 Vercel, Inc. Local changes adapt import paths and formatting; product-specific behavior stays in the tea wrapper.

The component is provided under [Apache License 2.0](licenses/Apache-2.0.txt), with the upstream [copyright notice](licenses/ai-elements-NOTICE.txt).

## Sub2API chat Markdown presentation

`src/components/tea/message-markdown.css` adapts the presentation rules in `tmp/sub2api/chat-ui/src/markdown.css` from [Aerisun/sub2api](https://github.com/Aerisun/sub2api/blob/47edf1aed42f0dac90fb53308a34807d2c547c45/chat-ui/src/markdown.css) at reference commit `47edf1aed42f0dac90fb53308a34807d2c547c45`. Local changes scope the rules to tea chat, adapt Streamdown selectors and theme variables, and retain the existing tea rendering and sanitization pipeline.

The adapted stylesheet is provided under [GNU Lesser General Public License 3.0](licenses/LGPL-3.0.txt), which incorporates [GNU General Public License 3.0](licenses/GPL-3.0.txt). Its editable source is included at the path above. These licenses apply to that stylesheet; other files retain their respective licenses.
