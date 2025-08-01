import os
import logging
import json
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from shapely.geometry import LineString, box
import math

# Configure logging with more detail
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Filter API v7.0.2 - Fixed Dimension Filtering",
    description="FIXED: Enhanced dimension filtering for gevel/doorsnede with P, V, + symbols",
    version="7.0.2"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Input models
class VectorLine(BaseModel):
    p1: List[float]
    p2: List[float] 
    stroke_width: float = Field(default=1.0)
    length: float
    color: List[int] = Field(default=[0, 0, 0])
    is_dashed: bool = Field(default=False)
    angle: Optional[float] = None

class VectorText(BaseModel):
    text: str
    position: List[float]
    font_size: Optional[float] = None
    bounding_box: List[float]

class VectorPage(BaseModel):
    page_size: Dict[str, float]
    lines: List[VectorLine]
    texts: List[VectorText]

class VectorData(BaseModel):
    page_number: int
    pages: List[VectorPage]

class VisionRegion(BaseModel):
    label: str
    coordinate_block: List[float]

class VisionOutput(BaseModel):
    drawing_type: str
    regions: List[VisionRegion]
    image_metadata: Optional[Dict] = None

class FilterInput(BaseModel):
    vector_data: VectorData
    vision_output: VisionOutput

# Output models - optimized for Scale API v7.0.0
class CleanPoint(BaseModel):
    x: float
    y: float

class FilteredLine(BaseModel):
    length: float
    orientation: str
    midpoint: CleanPoint

class FilteredText(BaseModel):
    text: str
    midpoint: Dict[str, float]
    
class RegionData(BaseModel):
    label: str
    lines: List[FilteredLine]
    texts: List[FilteredText]
    parsed_drawing_type: Optional[str] = None

class CleanOutput(BaseModel):
    drawing_type: str
    regions: List[RegionData]

def parse_bestektekening_region_type(region_label: str) -> str:
    """Extract drawing type from bestektekening region label"""
    
    # Check for explicit type in parentheses first
    if "(" in region_label and ")" in region_label:
        try:
            start = region_label.find("(") + 1
            end = region_label.find(")")
            extracted_type = region_label[start:end].strip()
            
            valid_types = [
                "plattegrond", "doorsnede", "gevelaanzicht", 
                "detailtekening_kozijn", "detailtekening_plattegrond",
                "detailtekening"
            ]
            
            if extracted_type in valid_types:
                logger.debug(f"Extracted drawing type '{extracted_type}' from label '{region_label}'")
                return extracted_type
                
        except Exception as e:
            logger.warning(f"Failed to parse parentheses in label '{region_label}': {e}")
    
    # Fallback to keyword matching
    label_lower = region_label.lower()
    
    if "plattegrond" in label_lower or "grond" in label_lower or "verdieping" in label_lower:
        return "plattegrond"
    elif "gevel" in label_lower or "aanzicht" in label_lower:
        return "gevelaanzicht"
    elif "doorsnede" in label_lower:
        return "doorsnede"
    elif "detail" in label_lower:
        if "kozijn" in label_lower or "raam" in label_lower or "deur" in label_lower:
            return "detailtekening_kozijn"
        else:
            return "detailtekening"
    else:
        return "unknown"

def calculate_orientation(p1: List[float], p2: List[float], angle: Optional[float] = None) -> str:
    """Calculate line orientation"""
    if angle is not None:
        normalized_angle = abs(angle % 180)
        if normalized_angle < 15 or normalized_angle > 165:
            return "vertical"
        elif 75 < normalized_angle < 105:
            return "horizontal"
        else:
            return "diagonal"
    else:
        dx = abs(p2[0] - p1[0])
        dy = abs(p2[1] - p1[1])
        
        if dx < 1:
            return "horizontal"
        elif dy < 1:
            return "vertical"
        else:
            angle_rad = math.atan2(dy, dx)
            angle_deg = math.degrees(angle_rad)
            if angle_deg < 15 or angle_deg > 165:
                return "vertical"
            elif 75 < angle_deg < 105:
                return "horizontal"
            else:
                return "diagonal"

def calculate_midpoint(p1: List[float], p2: List[float]) -> CleanPoint:
    """Calculate midpoint of a line"""
    return CleanPoint(
        x=(p1[0] + p2[0]) / 2,
        y=(p1[1] + p2[1]) / 2
    )

def calculate_text_midpoint(bbox: List[float]) -> Dict[str, float]:
    """Calculate midpoint of text bounding box"""
    return {
        "x": (bbox[0] + bbox[2]) / 2,
        "y": (bbox[1] + bbox[3]) / 2
    }

def extract_dimension_value(text: str) -> Optional[float]:
    """FIXED: Extract numeric dimension value from text with enhanced patterns"""
    text_clean = text.strip()
    
    # ENHANCED: Handle various dimension formats including P, V, + symbols
    patterns = [
        # Standard format: 2400mm, 3.5m, 250cm
        r'^(\d+(?:[,.]\d+)?)\s*(mm|cm|m)?$',
        
        # With + prefix: +7555, +3000, +6410
        r'^\+(\d+(?:[,.]\d+)?)\s*(mm|cm|m|p|v)?$',
        
        # With P suffix: 7555P, 3000P, 6410P  
        r'^(\d+(?:[,.]\d+)?)\s*[pP]\s*(mm|cm|m)?$',
        
        # With V suffix: 7555V, 3000V
        r'^(\d+(?:[,.]\d+)?)\s*[vV]\s*(mm|cm|m)?$',
        
        # Combined formats: +7555P, +3000P, +6410P, +7075P
        r'^\+(\d+(?:[,.]\d+)?)\s*[pPvV]\s*(mm|cm|m)?$',
        
        # With + suffix: 6032+p, 3749+p (space variations)
        r'^(\d+(?:[,.]\d+)?)\s*\+\s*[pPvV]\s*(mm|cm|m)?$',
        
        # Additional space variations: 6032 +p, 3749 + p
        r'^(\d+(?:[,.]\d+)?)\s+\+\s*[pPvV]\s*(mm|cm|m)?$'
    ]
    
    for pattern in patterns:
        match = re.match(pattern, text_clean, re.IGNORECASE)
        if match:
            value_str = match.group(1).replace(',', '.')
            value = float(value_str)
            
            # Get unit (if specified in match group 2)
            unit = match.group(2) if len(match.groups()) > 1 and match.group(2) else None
            
            # Convert to mm based on unit (ignore p/v/P/V - they're not units)
            if unit and unit.lower() in ['mm', 'cm', 'm']:
                if unit.lower() == 'cm':
                    return value * 10
                elif unit.lower() == 'm':
                    return value * 1000
                else:  # mm
                    return value
            else:
                # No unit specified or P/V suffix - assume mm
                return value
    
    return None

def is_valid_dimension(text: str, drawing_type: str = "general") -> bool:
    """FIXED: Enhanced validation for dimension text with drawing type specific rules"""
    if not text or not text.strip():
        return False
    
    text_clean = text.strip()
    
    # For specific drawing types that need enhanced dimension support
    enhanced_types = ["doorsnede", "gevelaanzicht", "bestektekening", "detailtekening_kozijn"]
    
    # Try to extract dimension value using enhanced patterns
    extracted_value = extract_dimension_value(text_clean)
    if extracted_value is None:
        return False
    
    # Drawing type specific validation
    if drawing_type == "plattegrond":
        min_value = 500  # Plattegrond dimensions usually larger
        if extracted_value < min_value:
            logger.debug(f"Dimension '{text_clean}' too small for plattegrond: {extracted_value}mm < {min_value}mm")
            return False
    else:
        min_value = 100  # Other types allow smaller dimensions
        if extracted_value < min_value:
            logger.debug(f"Dimension '{text_clean}' too small: {extracted_value}mm < {min_value}mm")
            return False
    
    # Maximum reasonable value check (100 meters)
    max_value = 100000
    if extracted_value > max_value:
        logger.debug(f"Dimension '{text_clean}' too large: {extracted_value}mm > {max_value}mm")
        return False
    
    logger.debug(f"Valid dimension found: '{text_clean}' = {extracted_value}mm for {drawing_type}")
    return True

def line_intersects_region(line_p1: List[float], line_p2: List[float], region: List[float]) -> bool:
    """Check if line intersects with expanded region (25pt buffer)"""
    x1, y1, x2, y2 = region
    expanded_region = [x1 - 25, y1 - 25, x2 + 25, y2 + 25]
    
    # Check if any endpoint is in expanded region
    for x, y in [line_p1, line_p2]:
        if expanded_region[0] <= x <= expanded_region[2] and expanded_region[1] <= y <= expanded_region[3]:
            return True
    
    # Check line bounds overlap
    line_x1, line_y1 = line_p1
    line_x2, line_y2 = line_p2
    
    line_min_x = min(line_x1, line_x2)
    line_max_x = max(line_x1, line_x2)
    line_min_y = min(line_y1, line_y2)
    line_max_y = max(line_y1, line_y2)
    
    if line_max_x < expanded_region[0] or line_min_x > expanded_region[2] or line_max_y < expanded_region[1] or line_min_y > expanded_region[3]:
        return False
    
    # Use Shapely for precise intersection
    try:
        line = LineString([line_p1, line_p2])
        region_box = box(expanded_region[0], expanded_region[1], expanded_region[2], expanded_region[3])
        return line.intersects(region_box)
    except Exception as e:
        logger.warning(f"Shapely intersection failed: {e}")
        return False

def text_overlaps_region(text: VectorText, region: List[float]) -> bool:
    """Check if text bounding box overlaps with expanded region (25pt buffer)"""
    x1, y1, x2, y2 = region
    expanded_region = [x1 - 25, y1 - 25, x2 + 25, y2 + 25]
    
    text_x1, text_y1, text_x2, text_y2 = text.bounding_box
    
    return not (text_x2 < expanded_region[0] or
                text_x1 > expanded_region[2] or
                text_y2 < expanded_region[1] or
                text_y1 > expanded_region[3])

def should_include_line(line: VectorLine, drawing_type: str, region_label: str) -> bool:
    """Enhanced filtering rules with bestektekening region parsing"""
    
    if drawing_type == "bestektekening":
        region_drawing_type = parse_bestektekening_region_type(region_label)
        
        if region_drawing_type == "plattegrond":
            return (line.stroke_width <= 1.5 and line.length >= 50) or line.is_dashed
        elif region_drawing_type == "gevelaanzicht":
            return (line.stroke_width <= 1.5 and line.length >= 40) or line.is_dashed
        elif region_drawing_type == "doorsnede":
            return (line.stroke_width <= 1.5 and line.length >= 40) or line.is_dashed
        elif region_drawing_type in ["detailtekening_kozijn", "detailtekening_plattegrond", "detailtekening"]:
            return (line.stroke_width <= 1.0 and line.length >= 20) or line.is_dashed
        else:
            return (line.stroke_width <= 1.5 and line.length >= 30) or line.is_dashed
    
    elif drawing_type == "plattegrond":
        return (line.stroke_width <= 1.5 and line.length >= 50) or line.is_dashed
    elif drawing_type == "gevelaanzicht":
        return (line.stroke_width <= 1.5 and line.length >= 40) or line.is_dashed
    elif drawing_type == "doorsnede":
        return (line.stroke_width <= 1.5 and line.length >= 40) or line.is_dashed
    elif drawing_type in ["detailtekening", "detailtekening_kozijn", "detailtekening_plattegrond"]:
        return (line.stroke_width <= 1.0 and line.length >= 20) or line.is_dashed
    elif drawing_type == "installatietekening":
        return False
    else:
        return (line.stroke_width <= 1.5 and line.length >= 30) or line.is_dashed

def should_include_text(text: VectorText, drawing_type: str, region_label: str) -> bool:
    """FIXED: Enhanced text filtering with bestektekening region parsing and enhanced dimension support"""
    
    if drawing_type == "bestektekening":
        region_drawing_type = parse_bestektekening_region_type(region_label)
        effective_drawing_type = region_drawing_type
    else:
        effective_drawing_type = drawing_type
    
    if effective_drawing_type == "installatietekening":
        return False
    
    # FIXED: Use enhanced dimension validation
    is_valid = is_valid_dimension(text.text, effective_drawing_type)
    
    if is_valid:
        logger.debug(f"Including dimension text: '{text.text}' for {effective_drawing_type}")
    else:
        logger.debug(f"Excluding text: '{text.text}' for {effective_drawing_type}")
    
    return is_valid

def remove_duplicate_lines(lines: List[VectorLine]) -> List[VectorLine]:
    """Remove duplicate lines based on coordinates with tolerance"""
    unique_lines = []
    tolerance = 1.0
    
    for line in lines:
        is_duplicate = False
        
        for existing_line in unique_lines:
            p1_match = (abs(line.p1[0] - existing_line.p1[0]) < tolerance and 
                       abs(line.p1[1] - existing_line.p1[1]) < tolerance)
            p2_match = (abs(line.p2[0] - existing_line.p2[0]) < tolerance and 
                       abs(line.p2[1] - existing_line.p2[1]) < tolerance)
            
            p1_reverse = (abs(line.p1[0] - existing_line.p2[0]) < tolerance and 
                         abs(line.p1[1] - existing_line.p2[1]) < tolerance)
            p2_reverse = (abs(line.p2[0] - existing_line.p1[0]) < tolerance and 
                         abs(line.p2[1] - existing_line.p1[1]) < tolerance)
            
            if (p1_match and p2_match) or (p1_reverse and p2_reverse):
                is_duplicate = True
                break
        
        if not is_duplicate:
            unique_lines.append(line)
    
    logger.info(f"Removed {len(lines) - len(unique_lines)} duplicate lines ({len(lines)} -> {len(unique_lines)})")
    return unique_lines

def convert_vector_drawing_api_format(raw_vector_data: Dict) -> VectorData:
    """Convert Vector Drawing API format to our internal format"""
    try:
        logger.info("=== Converting Vector Drawing API format ===")
        
        pages = raw_vector_data.get("pages", [])
        if not pages:
            raise ValueError("No pages found in vector data")
        
        logger.info(f"Found {len(pages)} pages")
        converted_pages = []
        
        for page_idx, page_data in enumerate(pages):
            page_size = page_data.get("page_size", {"width": 595.0, "height": 842.0})
            
            # Extract texts
            texts = []
            raw_texts = page_data.get("texts", [])
            logger.info(f"Found {len(raw_texts)} texts")
            
            for text_data in raw_texts:
                position = text_data.get("position", {"x": 0, "y": 0})
                bbox = text_data.get("bbox", {})
                
                if isinstance(position, dict):
                    pos_list = [float(position.get("x", 0)), float(position.get("y", 0))]
                else:
                    pos_list = [float(position[0]), float(position[1])]
                
                if isinstance(bbox, dict):
                    bbox_list = [
                        float(bbox.get("x0", 0)), 
                        float(bbox.get("y0", 0)), 
                        float(bbox.get("x1", 100)), 
                        float(bbox.get("y1", 20))
                    ]
                else:
                    bbox_list = [float(x) for x in bbox[:4]] if len(bbox) >= 4 else [0, 0, 100, 20]
                
                text = VectorText(
                    text=text_data.get("text", ""),
                    position=pos_list,
                    font_size=text_data.get("font_size", 12.0),
                    bounding_box=bbox_list
                )
                texts.append(text)
            
            # Extract lines
            lines = []
            possible_line_locations = [
                page_data.get("drawings", {}).get("lines", []),
                page_data.get("lines", []),
                page_data.get("paths", []),
                page_data.get("elements", [])
            ]
            
            drawings = page_data.get("drawings", {})
            if isinstance(drawings, list):
                possible_line_locations.append(drawings)
            
            found_lines = False
            for location_idx, line_list in enumerate(possible_line_locations):
                if line_list:
                    logger.info(f"Found {len(line_list)} lines in location {location_idx}")
                    found_lines = True
                    
                    for line_data in line_list:
                        if isinstance(line_data, dict) and (line_data.get("type") == "line" or "p1" in line_data):
                            p1 = line_data.get("p1", {"x": 0, "y": 0})
                            p2 = line_data.get("p2", {"x": 0, "y": 0})
                            
                            if isinstance(p1, dict):
                                p1_list = [float(p1.get("x", 0)), float(p1.get("y", 0))]
                            else:
                                p1_list = [float(p1[0]), float(p1[1])]
                            
                            if isinstance(p2, dict):
                                p2_list = [float(p2.get("x", 0)), float(p2.get("y", 0))]
                            else:
                                p2_list = [float(p2[0]), float(p2[1])]
                            
                            length = float(line_data.get("length", 0))
                            if length == 0:
                                dx = p2_list[0] - p1_list[0]
                                dy = p2_list[1] - p1_list[1]
                                length = math.sqrt(dx*dx + dy*dy)
                            
                            line = VectorLine(
                                p1=p1_list,
                                p2=p2_list,
                                stroke_width=float(line_data.get("width", line_data.get("stroke_width", 1.0))),
                                length=length,
                                color=line_data.get("color", [0, 0, 0]),
                                is_dashed=line_data.get("is_dashed", False),
                                angle=line_data.get("angle")
                            )
                            lines.append(line)
                    break
            
            if not found_lines:
                logger.warning("NO LINES FOUND IN ANY EXPECTED LOCATION!")
            
            page = VectorPage(
                page_size=page_size,
                lines=lines,
                texts=texts
            )
            converted_pages.append(page)
        
        result = VectorData(page_number=1, pages=converted_pages)
        
        total_lines = sum(len(page.lines) for page in converted_pages)
        total_texts = sum(len(page.texts) for page in converted_pages)
        logger.info(f"✅ Converted Vector Drawing API data: {total_lines} lines, {total_texts} texts")
        
        return result
        
    except Exception as e:
        logger.error(f"Error converting Vector Drawing API format: {e}", exc_info=True)
        raise ValueError(f"Failed to convert Vector Drawing API format: {str(e)}")

@app.post("/filter/", response_model=CleanOutput)
async def filter_clean(input_data: FilterInput, debug: bool = Query(False)):
    """Filter data and return clean, Scale API compatible output per region"""
    
    try:
        if not input_data.vector_data.pages:
            raise HTTPException(status_code=400, detail="No pages in vector_data")
        
        vector_page = input_data.vector_data.pages[0]
        drawing_type = input_data.vision_output.drawing_type
        regions = input_data.vision_output.regions
        
        logger.info(f"=== FILTERING START (v7.0.2 - FIXED DIMENSIONS) ===")
        logger.info(f"Processing {drawing_type} with {len(regions)} regions")
        logger.info(f"Input: {len(vector_page.lines)} lines, {len(vector_page.texts)} texts")
        logger.info(f"Debug mode: {debug}")
        logger.info(f"Enhanced dimension support: +7555P, 6032+p, etc.")
        
        # Skip installatietekening entirely
        if drawing_type == "installatietekening":
            logger.info("Skipping installatietekening - no processing")
            return CleanOutput(drawing_type=drawing_type, regions=[])
        
        # STEP 1: Filter ALL lines based on drawing type rules
        logger.info(f"STEP 1: Filtering all lines based on {drawing_type} rules...")
        
        if drawing_type == "bestektekening":
            # For bestektekening, apply permissive rules first
            filtered_lines = []
            for line in vector_page.lines:
                if (line.stroke_width <= 1.5 and line.length >= 20) or line.is_dashed:
                    filtered_lines.append(line)
        else:
            # For other drawing types, apply standard rules
            filtered_lines = []
            for line in vector_page.lines:
                if should_include_line(line, drawing_type, ""):
                    filtered_lines.append(line)
        
        logger.info(f"After drawing type filtering: {len(filtered_lines)} lines (was {len(vector_page.lines)})")
        
        # STEP 2: Remove duplicates from filtered lines  
        logger.info(f"STEP 2: Removing duplicates from filtered lines...")
        if len(filtered_lines) > 0:
            unique_filtered_lines = remove_duplicate_lines(filtered_lines)
        else:
            unique_filtered_lines = filtered_lines
        
        # STEP 3: Process regions with filtered lines
        logger.info(f"STEP 3: Processing regions with {len(unique_filtered_lines)} filtered lines...")
        
        region_outputs = []
        total_lines_included = 0
        total_valid_dimension_texts = 0
        
        for region in regions:
            region_lines = []
            region_texts = []
            
            logger.info(f"\nProcessing region: {region.label}")
            
            # Parse drawing type for bestektekening regions
            parsed_drawing_type = None
            if drawing_type == "bestektekening":
                parsed_drawing_type = parse_bestektekening_region_type(region.label)
                logger.info(f"  Parsed drawing type: {parsed_drawing_type}")
            
            # Process lines for this region
            lines_in_region = 0
            lines_passed_filter = 0
            
            for line in unique_filtered_lines:
                if line_intersects_region(line.p1, line.p2, region.coordinate_block):
                    lines_in_region += 1
                    
                    # For bestektekening, apply additional region-specific filtering
                    if drawing_type == "bestektekening":
                        if should_include_line(line, drawing_type, region.label):
                            total_lines_included += 1
                            lines_passed_filter += 1
                            filtered_line = FilteredLine(
                                length=line.length,
                                orientation=calculate_orientation(line.p1, line.p2, line.angle),
                                midpoint=calculate_midpoint(line.p1, line.p2)
                            )
                            region_lines.append(filtered_line)
                    else:
                        # For other drawing types, lines are already filtered
                        total_lines_included += 1
                        lines_passed_filter += 1
                        filtered_line = FilteredLine(
                            length=line.length,
                            orientation=calculate_orientation(line.p1, line.p2, line.angle),
                            midpoint=calculate_midpoint(line.p1, line.p2)
                        )
                        region_lines.append(filtered_line)
            
            logger.info(f"  Lines in region: {lines_in_region}, passed filter: {lines_passed_filter}")
            
            # Process texts for this region
            texts_in_region = 0
            texts_passed_filter = 0
            dimension_examples = []
            
            for text in vector_page.texts:
                if text_overlaps_region(text, region.coordinate_block):
                    texts_in_region += 1
                    
                    if should_include_text(text, drawing_type, region.label):
                        total_valid_dimension_texts += 1
                        texts_passed_filter += 1
                        dimension_examples.append(text.text)
                        
                        filtered_text = FilteredText(
                            text=text.text,
                            midpoint=calculate_text_midpoint(text.bounding_box)
                        )
                        region_texts.append(filtered_text)
            
            logger.info(f"  Texts in region: {texts_in_region}, passed filter: {texts_passed_filter}")
            if dimension_examples:
                logger.info(f"  Example dimensions: {dimension_examples[:5]}")
            
            # Create region output with parsed drawing type
            region_data = RegionData(
                label=region.label,
                lines=region_lines,
                texts=region_texts,
                parsed_drawing_type=parsed_drawing_type
            )
            region_outputs.append(region_data)
            
            logger.info(f"  {region.label} final results:")
            logger.info(f"    Lines included: {len(region_lines)}")
            logger.info(f"    Valid dimension texts: {len(region_texts)}")
        
        # Create clean output
        output = CleanOutput(
            drawing_type=drawing_type,
            regions=region_outputs
        )
        
        logger.info(f"\n=== FILTERING COMPLETE ===")
        logger.info(f"Total lines processed: {len(unique_filtered_lines)}")
        logger.info(f"Total lines included: {total_lines_included}")
        logger.info(f"Total valid dimension texts: {total_valid_dimension_texts}")
        logger.info(f"Regions processed: {len(region_outputs)}")
        
        return output
    
    except Exception as e:
        logger.error(f"Error in filter_clean: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/filter-from-vector-api/", response_model=CleanOutput)
async def filter_from_vector_api(
    vector_data: Dict[str, Any], 
    vision_output: Dict[str, Any],
    debug: bool = Query(False)
):
    """Direct endpoint that accepts raw Vector Drawing API output"""
    
    try:
        logger.info("=== Processing raw Vector Drawing API output ===")
        
        # Convert Vector Drawing API format to our internal format
        converted_vector_data = convert_vector_drawing_api_format(vector_data)
        
        # Create vision output object
        vision_obj = VisionOutput(**vision_output)
        
        # Create filter input
        filter_input = FilterInput(
            vector_data=converted_vector_data,
            vision_output=vision_obj
        )
        
        # Process using the main filter function
        return await filter_clean(filter_input, debug)
        
    except Exception as e:
        logger.error(f"Error processing Vector API output: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing Vector API output: {str(e)}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Filter API v7.0.2 - Enhanced Dimension Filtering",
        "version": "7.0.2",
        "timestamp": datetime.now().isoformat(),
        "pydantic_version": "1.10.18",
        "compatibility": "Scale API v7.1.0",
        "dimension_support": {
            "enhanced_patterns": [
                "+7555P", "+3000P", "+6410P", "+7075P",
                "6032+p", "3749+p", "7555P", "3000V",
                "2400mm", "3.5m", "250cm"
            ],
            "supported_suffixes": ["P", "V", "p", "v"],
            "supported_prefixes": ["+"],
            "supported_units": ["mm", "cm", "m"]
        }
    }

@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "title": "Filter API v7.0.2 - Enhanced Dimension Filtering",
        "version": "7.0.2",
        "description": "FIXED: Enhanced dimension filtering for gevel/doorsnede with P, V, + symbols",
        "bug_fix": {
            "issue": "Dimension filtering too strict - excluded P, V, + symbols used in gevel/doorsnede",
            "examples_now_supported": [
                "+7555P", "+3000P", "+6410P", "+7075P",
                "6032+p", "3749+p", "7555P", "3000V",
                "2400", "3.5m", "250cm"
            ],
            "regex_patterns": "7 enhanced patterns for various dimension formats"
        },
        "enhanced_dimension_patterns": {
            "standard": "2400mm, 3.5m, 250cm",
            "with_plus_prefix": "+7555, +3000, +6410", 
            "with_p_suffix": "7555P, 3000P, 6410P",
            "with_v_suffix": "7555V, 3000V",
            "combined_formats": "+7555P, +3000P, +6410P, +7075P",
            "plus_suffix": "6032+p, 3749+p",
            "space_variations": "6032 +p, 3749 + p"
        },
        "drawing_type_specific": {
            "plattegrond": "Min 500mm (larger dimensions)",
            "doorsnede": "Min 100mm + enhanced P/V/+ support",
            "gevelaanzicht": "Min 100mm + enhanced P/V/+ support", 
            "detailtekening_kozijn": "Min 100mm + enhanced P/V/+ support",
            "bestektekening": "Region-specific rules with enhanced support"
        },
        "correct_workflow": [
            "1. Filter ALL lines based on drawing type rules (stroke width + length)",
            "2. Remove duplicates from filtered lines", 
            "3. Check which filtered lines fall in each region (25pt buffer)",
            "4. For bestektekening: apply additional region-specific filtering",
            "5. Filter texts for valid dimensions with ENHANCED pattern matching"
        ],
        "compatibility": {
            "filter_api": "Pydantic v1.10.18",
            "scale_api": "Pydantic v2.6.4 + v7.1.0 distance rules",
            "dimension_extraction": "Enhanced for gevel/doorsnede requirements"
        },
        "filtering_rules": {
            "plattegrond": "stroke ≤1.5pt, length ≥50pt, dimensions ≥500mm",
            "gevelaanzicht": "stroke ≤1.5pt, length ≥40pt, dimensions ≥100mm + P/V/+", 
            "doorsnede": "stroke ≤1.5pt, length ≥40pt, dimensions ≥100mm + P/V/+",
            "detailtekening_kozijn": "stroke ≤1.0pt, length ≥20pt, dimensions ≥100mm + P/V/+",
            "bestektekening": "permissive global filter, then region-specific + enhanced dimensions",
            "installatietekening": "SKIPPED - no processing"
        },
        "endpoints": {
            "/filter/": "Main filtering endpoint (add ?debug=true for detailed output)",
            "/filter-from-vector-api/": "Direct endpoint for Vector Drawing API output",
            "/health": "Health check with enhanced dimension support info",
            "/": "This comprehensive documentation endpoint"
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting Filter API v7.0.2-ENHANCED-DIMENSIONS on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
