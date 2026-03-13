export const mockMenu = {
  categories: [
    { id: "cat-bowls", name: "Bowls", sort: 1 },
    { id: "cat-sandwiches", name: "Sandwiches", sort: 2 },
    { id: "cat-drinks", name: "Drinks", sort: 3 },
  ],
  items: [
    {
      id: "item-1",
      categoryId: "cat-bowls",
      name: "Rooted Power Bowl",
      description: "Greens, grains, roasted veg, tahini drizzle.",
      basePriceCents: 1299,
      modifierGroups: [
        {
          id: "mg-protein",
          name: "Choose a protein",
          minSelect: 1,
          maxSelect: 1,
          options: [
            { id: "opt-tofu", name: "Crispy Tofu", priceDeltaCents: 0 },
            { id: "opt-tempeh", name: "Smoky Tempeh", priceDeltaCents: 150 },
          ],
        },
        {
          id: "mg-extras",
          name: "Extras",
          minSelect: 0,
          maxSelect: 3,
          options: [
            { id: "opt-avocado", name: "Avocado", priceDeltaCents: 200 },
            { id: "opt-pickled", name: "Pickled Onions", priceDeltaCents: 50 },
            { id: "opt-jalapeno", name: "Jalapeno", priceDeltaCents: 0 },
          ],
        },
      ],
    },
    {
      id: "item-2",
      categoryId: "cat-sandwiches",
      name: "Garden Melt",
      description: "Grilled veg + melty vegan cheese on sourdough.",
      basePriceCents: 1099,
      modifierGroups: [
        {
          id: "mg-side",
          name: "Side",
          minSelect: 1,
          maxSelect: 1,
          options: [
            { id: "opt-chips", name: "Kettle Chips", priceDeltaCents: 0 },
            { id: "opt-salad", name: "Side Salad", priceDeltaCents: 200 },
          ],
        },
      ],
    },
    {
      id: "item-3",
      categoryId: "cat-drinks",
      name: "Citrus Iced Tea",
      basePriceCents: 399,
      modifierGroups: [
        {
          id: "mg-ice",
          name: "Ice",
          minSelect: 1,
          maxSelect: 1,
          options: [
            { id: "opt-regular-ice", name: "Regular", priceDeltaCents: 0 },
            { id: "opt-light-ice", name: "Light", priceDeltaCents: 0 },
            { id: "opt-no-ice", name: "No Ice", priceDeltaCents: 0 },
          ],
        },
      ],
    },
  ],
};
