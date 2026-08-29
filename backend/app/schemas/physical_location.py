from pydantic import BaseModel, Field


class PhysicalLocationBlockCreate(BaseModel):
    name: str = Field(min_length=1)


class PhysicalLocationDataCenterCreate(BaseModel):
    block: str = Field(min_length=1)
    name: str = Field(min_length=1)


class PhysicalLocationRackCreate(BaseModel):
    block: str = Field(min_length=1)
    data_center: str = Field(min_length=1)
    name: str = Field(min_length=1)


class PhysicalLocationRename(BaseModel):
    name: str = Field(min_length=1)
