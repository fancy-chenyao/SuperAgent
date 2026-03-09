import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from rich.console import Console

console = Console()

def send_email_with_attachment(file_path):
    # 配置信息
    sender_email = "fancy_chenyao@163.com"
    receiver_email = "15568752689@163.com"
    smtp_server = "smtp.163.com"
    smtp_port = 465
    auth_code = "RFZ2C3NHrPpibiNS"

    # 检查文件是否存在
    if not os.path.exists(file_path):
        console.print(f"[bold red]错误：[/bold red] 文件 [yellow]{file_path}[/yellow] 不存在！")
        return

    # 创建邮件对象
    message = MIMEMultipart()
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Subject"] = f"文件发送：{os.path.basename(file_path)}"

    # 邮件正文
    body = "您好，附件中是您需要的文件，请查收。"
    message.attach(MIMEText(body, "plain"))

    try:
        # 添加附件
        console.print(f"[blue]正在准备附件：[/blue] {file_path}")
        with open(file_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
        
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename= {os.path.basename(file_path)}",
        )
        message.attach(part)

        # 连接服务器并发送
        console.print(f"[blue]正在连接 SMTP 服务器 {smtp_server}:{smtp_port}...[/blue]")
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(sender_email, auth_code)
            console.print("[green]登录成功！[/green]")
            server.send_message(message)
            console.print(f"[bold green]邮件发送成功！[/bold green] 已发送至 [cyan]{receiver_email}[/cyan]")

    except Exception as e:
        console.print(f"[bold red]发送失败：[/bold red] {str(e)}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        console.print("[yellow]用法：[/yellow] python send_email_163.py <文件路径>")
    else:
        target_file = sys.argv[1]
        send_email_with_attachment(target_file)
