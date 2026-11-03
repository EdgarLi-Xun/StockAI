# 消费账号验证和密码重置邮件队列，限制重试并清理令牌载荷。
from __future__ import annotations

import json
import smtplib
import ssl
from email.message import EmailMessage

from .auth import later
from .store import stamp


# 领取一封到期且未过期的邮件，发送后更新状态并清理敏感内容。
def send_one(store,keys,sender=None):
    cfg=store.cfg
    with store.tx(immediate=True) as c:
        if c.execute("SELECT value FROM cloud_meta WHERE key='maintenance'").fetchone()[0]!='0':return False
        row=c.execute("SELECT m.*,u.email,u.status FROM mail_outbox m JOIN users u ON u.id=m.user_id WHERE m.state IN ('queued','retry') AND m.next_at<=? ORDER BY m.created_at LIMIT 1",(stamp(),)).fetchone()
        if not row:
            return False
        row=dict(row)
        if row["status"]=="disabled" or row["expires_at"]<=stamp():
            c.execute("UPDATE mail_outbox SET state='expired',payload_json=NULL WHERE id=?",(row["id"],))
            return True
        if sender is None and not all((cfg.smtp_host,cfg.smtp_from)):
            c.execute("UPDATE mail_outbox SET next_at=?,error_code='SMTP_NOT_CONFIGURED' WHERE id=?",(later(300),row["id"]))
            return False
        c.execute("UPDATE mail_outbox SET state='sending',attempts=attempts+1 WHERE id=?",(row["id"],))
    try:
        data=json.loads(keys.open(row["payload_json"],site=store.site_id,user=row["user_id"],resource=row["id"],purpose="mail"))
        action="verify-email" if data["purpose"]=="verify" else "reset-password"
        url=cfg.public_url+"/#"+action+"?token="+data["token"]
        message=EmailMessage()
        message["Subject"]="StockAI 邮箱验证" if action=="verify-email" else "StockAI 重置密码"
        message["From"]=cfg.smtp_from or "no-reply@stockai.test"
        message["To"]=row["email"]
        message.set_content("请打开以下站点链接完成操作：\n"+url+"\n链接仅能使用一次；若非本人操作，请忽略此邮件。")
        if sender:
            sender(message)
        elif cfg.smtp_starttls:
            with smtplib.SMTP(cfg.smtp_host,cfg.smtp_port,timeout=15) as server:
                server.starttls(context=ssl.create_default_context())
                if cfg.smtp_user:
                    server.login(cfg.smtp_user,cfg.smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP_SSL(cfg.smtp_host,cfg.smtp_port,timeout=15,context=ssl.create_default_context()) as server:
                if cfg.smtp_user:
                    server.login(cfg.smtp_user,cfg.smtp_password)
                server.send_message(message)
        store.execute("UPDATE mail_outbox SET state='sent',payload_json=NULL,error_code=NULL WHERE id=?",(row["id"],))
    except Exception:
        # SMTP 原始错误可能含邮箱或令牌，只持久化脱敏错误码。
        store.execute("UPDATE mail_outbox SET state=CASE WHEN attempts<3 THEN 'retry' ELSE 'failed' END,next_at=?,error_code='SMTP_DELIVERY_FAILED' WHERE id=?",(later(120),row["id"]))
    return True
