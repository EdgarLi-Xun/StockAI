# 管理每位用户独立的研究预算、费用和调度偏好。
import json

from pydantic import BaseModel,ConfigDict,Field,field_validator

from backend.config import Fees,Settings
from .store import json_text


# 定义个人预算、费用和自动任务参数，限制允许修改的字段。
class Preferences(BaseModel):
    model_config=ConfigDict(extra="forbid")
    budget:float=Field(1000,gt=0,le=1000)
    fees:Fees=Field(default_factory=Fees)
    min_daily_amount:float=Field(1000000,ge=0,le=1e12)
    max_risk_age_hours:int=Field(24,ge=1,le=72)
    morning_enabled:bool=False
    closing_enabled:bool=False
    weekly_enabled:bool=False
    automatic_ai:bool=False
    morning_time:str="08:00"
    closing_time:str="16:00"
    weekly_time:str="20:00"

    # 校验个人计划采用合法的小时和分钟格式。
    @field_validator("morning_time","closing_time","weekly_time")
    @classmethod
    def valid_time(cls,v):
        return Settings.check_time(v)


# 按用户读取并校验个人偏好，不允许省略用户归属。
def get_preferences(store,uid):
    if not uid:
        raise ValueError("必须指定用户")
    row=store.one("SELECT payload_json FROM user_settings WHERE user_id=?",(uid,))
    if not row:
        raise ValueError("用户设置不存在")
    return Preferences.model_validate_json(row["payload_json"])


# 在确认账号有效后保存个人偏好，仅影响后续任务快照。
def save_preferences(store,uid,value):
    prefs=Preferences.model_validate(value)
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise ValueError("账号不可用")
        c.execute("UPDATE user_settings SET payload_json=? WHERE user_id=?",(prefs.model_dump_json(),uid))
    return prefs
