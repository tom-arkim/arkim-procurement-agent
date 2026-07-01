"""
Representative bounce/DSN fixtures for the bounce parser tests (RFC 3464 style,
Gmail mailer-daemon shape). Plain strings — no I/O, no live mail.

  HARD_BOUNCE    — permanent failure (Action: failed / 5.1.1 no such user), with the
                   original message attached so Message-ID is recoverable.
  SOFT_BOUNCE    — transient failure (Action: delayed / 4.2.2 mailbox full).
  NON_BOUNCE     — an ordinary vendor reply (must NOT be read as a bounce).
  MALFORMED_DSN  — looks bounce-ish but is broken (Action present, no Final-Recipient)
                   -> parser must return None, never crash or fabricate a recipient.
"""

HARD_BOUNCE = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: sourcing@arkim.ai
Subject: Delivery Status Notification (Failure)
MIME-Version: 1.0
Message-ID: <dsn-own-aaa@mail.gmail.com>
In-Reply-To: <rfq-abc@arkim.ai>
References: <rfq-abc@arkim.ai>
Content-Type: multipart/report; report-type=delivery-status; boundary="b1"

--b1
Content-Type: text/plain; charset="UTF-8"

Delivery to the following recipient failed permanently:

     sales@baypower.com

The email account that you tried to reach does not exist.

--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; sales@baypower.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.

--b1
Content-Type: message/rfc822

Message-ID: <rfq-abc@arkim.ai>
From: sourcing@arkim.ai
To: sales@baypower.com
Subject: Quote request - Bay Power

Hello, please quote PN EM3770T.
--b1--
"""

SOFT_BOUNCE = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: sourcing@arkim.ai
Subject: Delivery Status Notification (Delay)
MIME-Version: 1.0
Message-ID: <dsn-own-bbb@mail.gmail.com>
In-Reply-To: <rfq-def@arkim.ai>
Content-Type: multipart/report; report-type=delivery-status; boundary="b2"

--b2
Content-Type: text/plain; charset="UTF-8"

Delivery to the following recipient has been delayed:

     sales@standardelectricsupply.com

--b2
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; sales@standardelectricsupply.com
Action: delayed
Status: 4.2.2
Diagnostic-Code: smtp; 452 4.2.2 The recipient's mailbox is full.

--b2
Content-Type: message/rfc822

Message-ID: <rfq-def@arkim.ai>
From: sourcing@arkim.ai
To: sales@standardelectricsupply.com
Subject: Quote request - Standard Electric

Hello, please quote.
--b2--
"""

NON_BOUNCE = """\
From: Jane Baker <jeff@baypower.com>
To: sourcing@arkim.ai
Subject: Re: Quote request - Bay Power
Message-ID: <reply-001@baypower.com>
Content-Type: text/plain; charset="UTF-8"

Hi - yes, we can supply the EM3770T. Unit price $1,210, 5 business days,
FOB origin. Let me know if you'd like to proceed.

Thanks,
Jeff
"""

MALFORMED_DSN = """\
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: sourcing@arkim.ai
Subject: Delivery Status Notification (Failure)
Content-Type: multipart/report; report-type=delivery-status; boundary="b3"

--b3
Content-Type: message/delivery-status

Action: failed
Status: 5.0.0
(report truncated here - no Final-Recipient line at all)
--b3--
"""
