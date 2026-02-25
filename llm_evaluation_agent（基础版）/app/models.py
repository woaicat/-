"""Shared pydantic models used by the service."""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class WorkflowNode(BaseModel):
    """Describes a single step in the product workflow."""

    name: str = Field(..., description="Node name or label.")
    input_spec: str = Field(..., description="Expected inputs for this node.")
    output_spec: str = Field(..., description="Expected outputs for this node.")
    notes: Optional[str] = Field(
        default=None, description="Additional context, constraints or tools involved."
    )


class EvaluationRequest(BaseModel):
    """Payload submitted by the front-end form (merged with document summary)."""

    product_name: str = Field(..., description="Name of the product under evaluation.")
    application_scenario: Optional[str] = Field(
        default=None, description="The business scenario where the product is used."
    )
    target_users: Optional[str] = Field(
        default=None, description="Primary user personas."
    )
    business_goals: Optional[str] = Field(
        default=None, description="Business objectives the product is meant to achieve."
    )
    core_features: List[str] = Field(
        default_factory=list, description="Key product features."
    )
    workflow: List[WorkflowNode] = Field(
        default_factory=list, description="Detailed workflow nodes."
    )
    constraints: Optional[str] = Field(
        default=None, description="Special notes, risks or non-functional requirements."
    )
    document_notes: Optional[str] = Field(
        default=None,
        description="Important context or insights extracted from uploaded documents.",
    )


class StepStatus(str, Enum):
    """Event status values used by SSE payloads."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StreamEvent(BaseModel):
    """Entry pushed to the front-end during plan generation."""

    request_id: str
    step: str
    status: StepStatus
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    step_id: Optional[str] = None
    phase: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class GeneratedFile(BaseModel):
    """Metadata for a generated artefact."""

    filename: str
    path: Path
    download_url: str
    preview_url: Optional[str] = None
    description: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


class Manifest(BaseModel):
    """Aggregated results for a completed request."""

    request_id: str
    product_name: str
    created_at: datetime
    files: List[GeneratedFile]


class DocumentSummary(BaseModel):
    """Structured information extracted from uploaded product documents."""

    application_scenario: Optional[str] = None
    target_users: Optional[str] = None
    business_goals: Optional[str] = None
    core_features: List[str] = Field(default_factory=list)
    workflow: List[WorkflowNode] = Field(default_factory=list)
    constraints: Optional[str] = None
    notes: Optional[str] = None


class GeneratePlanResponse(BaseModel):
    """API response once plan generation completes."""

    request_id: str
    status: Literal["success", "error"]
    manifest: Optional[Manifest] = None
    error: Optional[str] = None
