from __future__ import annotations

from typing import List

from pydantic import BaseModel


class NewsletterLink(BaseModel):
    title: str
    description: str
    url: str
    posted_by: str


class NewsletterGroup(BaseModel):
    title: str
    links: List[NewsletterLink]


class NewsletterPayload(BaseModel):
    groups: List[NewsletterGroup]
