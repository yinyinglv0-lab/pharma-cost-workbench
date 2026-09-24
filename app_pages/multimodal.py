"""多模态分析页：上传图片 → 视觉模型结构化理解 → 结果标注与下载。

导航与页面配置属于 enterprise_app.py；本页不写库、不发任务、不写正式报告。
所有结果默认"模型输出、未经人工确认"；密钥只走环境变量，页面不接收 Key/地址。
"""
import json

import streamlit as st

from enterprise.multimodal import ALLOWED_TYPES, TASKS, provider_config

st.title('多模态辅助理解')
st.caption('工艺流程图 · 配方表格 · 设备铭牌 · 扫描件页面（视觉模型，OpenAI 兼容）')

try:
    provider, model, api_key, base_url = provider_config()
    st.caption(f'当前视觉供应商：{provider} · {model}'
               + ('（密钥已配置）' if api_key else '（⚠️ 密钥未配置，仅可预览页面）'))
except ValueError as exc:
    st.error(str(exc))
    st.stop()

task = st.selectbox('理解任务', list(TASKS), key='mm_task')
uploaded = st.file_uploader('上传图片（png / jpg / webp，≤10MB）',
                            type=['png', 'jpg', 'jpeg', 'webp'], key='mm_file')
if uploaded is not None:
    st.image(uploaded, width=560)
    if st.button('开始理解', type='primary', key='mm_run', disabled=not api_key):
        from enterprise.multimodal import analyze_image
        with st.spinner('视觉模型理解中…'):
            result = analyze_image(uploaded.getvalue(), uploaded.type, task)
        if result['ok']:
            st.success('理解完成（模型输出，未经人工确认）')
            st.text_area('理解结果', result['text'], height=320, key='mm_out')
            st.caption(f"供应商 {result['meta'].get('provider')} · "
                       f"模型 {result['meta'].get('model')} · "
                       f"耗时 {result['meta'].get('latency_seconds')} 秒 · "
                       f"输入 {result['meta'].get('prompt_tokens')} token · "
                       f"输出 {result['meta'].get('completion_tokens')} token")
            st.download_button('下载结果 JSON',
                               json.dumps(result, ensure_ascii=False, indent=2),
                               file_name='multimodal_result.json',
                               key='mm_download')
            st.info('⚠️ ' + result['disclaimer']
                    + '如需纳入知识库，请在"知识文档库"按确认→发布流程登记，'
                      '不得把本页结果直接写入报告。')
        else:
            st.error(result.get('reason', '调用失败'))
else:
    st.info('上传图片后点击"开始理解"。结果不自动入库、不写入正式报告，'
            '使用前需人工确认。')
