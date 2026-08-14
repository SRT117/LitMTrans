local default_image_width = "50%"

function Image(img)
  if img.attributes["width"] == nil and img.attributes["height"] == nil then
    img.attributes["width"] = default_image_width
  end
  return img
end

function Table(tbl)
  if tbl.attr and tbl.attr.classes then
    for _, class in ipairs(tbl.attr.classes) do
      if class == "export-table" then
        return tbl
      end
    end
    tbl.attr.classes:insert("export-table")
  end
  return tbl
end
