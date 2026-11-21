# 消费账号验证和密码重置邮件队列，限制重试并清理令牌载荷。
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage

from .auth import later
from .smtp_settings import configured, effective, transmit
from .store import json_text, stamp

TASK_FAILURE = "task_failure"


class TaskMailCancelled(Exception):
    pass


def task_mail_enabled(conn, uid):
    row = conn.execute("""SELECT json_extract(s.payload_json,'$.task_failure_email_enabled') enabled
        FROM user_settings s JOIN users u ON u.id=s.user_id
        WHERE s.user_id=? AND u.status='active' AND u.verified_at IS NOT NULL""", (uid,)).fetchone()
    return bool(row and row["enabled"])


def queue_task_failure(store, keys, conn, uid, job_id, notice, ts):
    if not (keys and task_mail_enabled(conn, uid) and configured(store)):
        return
    ident = f"task-failure:{job_id}"
    site_id = conn.execute("SELECT value FROM cloud_meta WHERE key='site_id'").fetchone()[0]
    payload = keys.seal(json_text({"notice": notice}), site=site_id, user=uid, resource=ident, purpose="mail")
    conn.execute("""INSERT INTO mail_outbox(id,user_id,purpose,payload_json,state,next_at,expires_at,created_at)
        VALUES(?,?,?,?,'queued',?,?,?) ON CONFLICT(id) DO NOTHING""",
                 (ident, uid, TASK_FAILURE, payload, ts, later(86400), ts))


def _send_task_failure(store, row, message, account, sender):
    attempted = False

    def before_send():
        nonlocal attempted
        with store.tx() as conn:
            if not task_mail_enabled(conn, row["user_id"]):
                raise TaskMailCancelled
        if not sender and not configured(store):
            raise TaskMailCancelled
        attempted = True

    try:
        if sender:
            before_send()
            sender(message)
        else:
            transmit(account, message, before_send)
    except TaskMailCancelled:
        state, error = "cancelled", None
    except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused) as exc:
        codes = [v[0] for v in exc.recipients.values()] if isinstance(exc, smtplib.SMTPRecipientsRefused) else [exc.smtp_code]
        state = "retry" if row["attempts"] < 2 and codes and all(400 <= n < 500 for n in codes) else "failed"
        error = "SMTP_REJECTED"
    except Exception:
        state = "uncertain" if attempted else "retry" if row["attempts"] < 2 else "failed"
        error = "DELIVERY_UNCERTAIN" if attempted else "SMTP_CONNECT_FAILED"
    else:
        state, error = "sent", None
    store.execute("""UPDATE mail_outbox SET state=?,error_code=?,next_at=?,
        payload_json=CASE WHEN ?='retry' THEN payload_json ELSE NULL END WHERE id=? AND state='sending'""",
                  (state, error, later(120), state, row["id"]))


# 领取一封到期且未过期的邮件，发送后更新状态并清理敏感内容。
def send_one(store,keys,sender=None):
    cfg=store.cfg
    account=None if sender else effective(store,keys)
    with store.tx(immediate=True) as c:
        if c.execute("SELECT value FROM cloud_meta WHERE key='maintenance'").fetchone()[0]!='0':return False
        row=c.execute("SELECT m.*,u.email,u.status FROM mail_outbox m JOIN users u ON u.id=m.user_id WHERE m.state IN ('queued','retry') AND m.next_at<=? ORDER BY m.created_at LIMIT 1",(stamp(),)).fetchone()
        if not row:
            return False
        row=dict(row)
        if row["status"]=="disabled" or row["expires_at"]<=stamp():
            c.execute("UPDATE mail_outbox SET state='expired',payload_json=NULL WHERE id=?",(row["id"],))
            return True
        if row["purpose"]==TASK_FAILURE and not task_mail_enabled(c, row["user_id"]):
            c.execute("UPDATE mail_outbox SET state='cancelled',payload_json=NULL WHERE id=?",(row["id"],))
            return True
        if sender is None and account is None:
            c.execute("UPDATE mail_outbox SET next_at=?,error_code='SMTP_NOT_CONFIGURED' WHERE id=?",(later(300),row["id"]))
            return False
        c.execute("UPDATE mail_outbox SET state='sending',attempts=attempts+1 WHERE id=?",(row["id"],))
    try:
        data=json.loads(keys.open(row["payload_json"],site=store.site_id,user=row["user_id"],resource=row["id"],purpose="mail"))
        message=EmailMessage()
        message["From"]=(account.sender if account else cfg.smtp_from) or "no-reply@stockai.test"
        message["To"]=row["email"]
        if row["purpose"]==TASK_FAILURE:
            message["Subject"]="StockAI 研究任务失败"
            message.set_content(data["notice"]+"\n\n在 StockAI 任务中心查看执行记录和可用的重试操作。\n")
            _send_task_failure(store,row,message,account,sender)
            return True
        action="verify-email" if data["purpose"]=="verify" else "reset-password"
        url=cfg.public_url+"/#"+action+"?token="+data["token"]
        message["Subject"]="StockAI 邮箱验证" if action=="verify-email" else "StockAI 重置密码"
        message.set_content("请打开以下站点链接完成操作：\n"+url+"\n链接仅能使用一次；若非本人操作，请忽略此邮件。")
        if sender:
            sender(message)
        else:
            transmit(account, message)
        store.execute("UPDATE mail_outbox SET state='sent',payload_json=NULL,error_code=NULL WHERE id=?",(row["id"],))
    except Exception:
        # SMTP 原始错误可能含邮箱或令牌，只持久化脱敏错误码。
        store.execute("UPDATE mail_outbox SET state=CASE WHEN attempts<3 THEN 'retry' ELSE 'failed' END,next_at=?,error_code='SMTP_DELIVERY_FAILED' WHERE id=?",(later(120),row["id"]))
    return True
