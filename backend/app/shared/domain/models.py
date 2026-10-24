from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


Tag = Annotated[str, Field(min_length=1, max_length=80)]
PublicURL = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"^https://")]
