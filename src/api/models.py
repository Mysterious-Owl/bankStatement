"""Request/response models shared by the routers."""
from typing import Dict, List, Optional

from pydantic import BaseModel


class Category(BaseModel):
    id: str
    name: str
    color: str = "#3b82f6"
    icon: Optional[str] = None


class Rule(BaseModel):
    id: str
    name: str
    pattern: str
    category_id: str
    priority: int = 0
    extraction_fields: Optional[List[str]] = []


class TestRuleRequest(BaseModel):
    pattern: str


class ApplyTagsRequest(BaseModel):
    group_key: str = ""
    tags: List[str]
    create_rule: bool = True
    grouping_columns: List[str] = []
    entity_values: Optional[Dict[str, str]] = None
    # Structured form of group_key. Preferred, because the delimited string
    # cannot round-trip values that contain a pipe.
    criteria: Optional[Dict[str, str]] = None


class TransactionTagRequest(BaseModel):
    # Accepts either an anno_key or a legacy TxnID -- aliases resolve both.
    txn_id: str
    tags: List[str]
    source: str = "manual"
    mode: str = "add"  # add | remove | set


class CategoryTagsRequest(BaseModel):
    category_id: str
    tags: List[str]


class RemarkUpdateRequest(BaseModel):
    txn_id: str
    remarks: str


class AnnotateRequest(BaseModel):
    anno_key: str
    # Any field left as None is untouched, so a caller can update just the
    # remarks without having to send the tag list back.
    tags: Optional[List[str]] = None
    add_tags: Optional[List[str]] = None
    remove_tags: Optional[List[str]] = None
    remarks: Optional[str] = None
    description_override: Optional[str] = None
    context: Optional[Dict] = None


class BulkAnnotateRequest(BaseModel):
    anno_keys: List[str]
    add_tags: Optional[List[str]] = None
    remove_tags: Optional[List[str]] = None
    remarks: Optional[str] = None


class RenameTagRequest(BaseModel):
    old_name: str
    new_name: str


class DeleteTagRequest(BaseModel):
    name: str


class ProcessUploadRequest(BaseModel):
    upload_id: str
    mappings: Dict[str, Dict[str, str]]
    banks: Dict[str, str]


class SelfTransferSaveRequest(BaseModel):
    pair_id: str
    debit_key: str
    credit_key: str
    amount: float = 0
    debit_date: str = ""
    credit_date: str = ""
    from_bank: str = ""
    to_bank: str = ""


class UnmarkTransferRequest(BaseModel):
    pair_id: str


class RelinkRequest(BaseModel):
    orphan_key: str
    target_key: str


class ReviewRequest(BaseModel):
    anno_keys: List[str]
    reviewed: bool = True


class IgnoreRowsRequest(BaseModel):
    # row keys are "<anno_key>|<source_file>" -- see annotations.row_key
    row_keys: List[str]
    ignored: bool = True
    reason: str = "duplicate"


class DismissDuplicateRequest(BaseModel):
    group_id: str


class Budget(BaseModel):
    id: Optional[str] = None
    scope: str = "category"  # category | tag
    target: str
    amount: float
    rollover: bool = False
    # YYYY-MM the rollover accumulates from; defaults to the month it was created
    start_month: Optional[str] = None
