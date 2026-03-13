import { Cart, Menu, Pricing } from "./types";

export interface MenuProvider {
  getMenu(): Promise<Menu>;
}

export interface PricingProvider {
  priceCart(args: { menu: Menu; cart: Cart }): Promise<Pricing>;
}

export interface OrderSubmitter {
  submit(args: { menu: Menu; cart: Cart; pricing: Pricing }): Promise<{ orderId: string }>;
}
