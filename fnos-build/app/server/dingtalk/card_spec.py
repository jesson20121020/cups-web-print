"""互动卡片模板定义。

字段都用 ${变量} 占位，投放/更新时通过 cardParamMap 传值。
按钮的 callbackId 会在 Stream 的卡片回调里回传，用来区分「确认打印 / 取消」。
"""

TEMPLATE_NAME = "家里打印机-打印确认卡片"


def schema() -> dict:
    return {
        "config": {
            "autoLayout": True,
            "enableForward": True,
        },
        "header": {
            "title": {"type": "text", "text": "${title}"},
            "logo": "@lALPDfYH6Ex1aTPNA8HNA8H"},
        "contents": [
            {
                "type": "markdown",
                "id": "body",
                "text": "${content}",
            },
            {
                "type": "action",
                "id": "buttons",
                "actions": [
                    {
                        "type": "button",
                        "id": "btn_confirm",
                        "text": "确认打印",
                        "style": "primary",
                        "actionType": "callback",
                        "callbackId": "confirm",
                    },
                    {
                        "type": "button",
                        "id": "btn_cancel",
                        "text": "取消",
                        "style": "default",
                        "actionType": "callback",
                        "callbackId": "cancel",
                    },
                ],
            },
        ],
    }


# 卡片里用到的变量（建模板时平台要能识别，更新时按这个顺序传）
PARAMS = ["title", "content"]
